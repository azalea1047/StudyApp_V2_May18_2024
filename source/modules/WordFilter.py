import logging
import fitz  # PyMuPDF
from langchain.text_splitter import RecursiveCharacterTextSplitter
import sqlite3
import concurrent.futures
import time
import os
import re
from collections import defaultdict
from modules.path import database_log

# Setup logging to log messages to a file, with the option to reset the log file
def setup_logging(log_file= database_log):
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        filemode='a'  # Append to the log file for 'a', or overwrite for 'w'
    )

setup_logging()

# Function to extract text from a PDF file using PyMuPDF with improved error handling
def extract_text_from_pdf(pdf_file):
    logging.info(f"Extracting text from {pdf_file}...")
    text = ""
    try:
        doc = fitz.open(pdf_file)
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            page_text = page.get_text()
            # Log first 50 characters for debugging
            logging.debug(f"Extracted text from page {page_num} of {pdf_file}: {page_text[:50]}...")
            text += page_text
    except fitz.fitz_error as e:  # Specific MuPDF error
        logging.error(f"MuPDF error in {pdf_file}: {e}")
    except Exception as e:
        logging.error(f"Error extracting text from {pdf_file}: {e}")
    finally:
        if 'doc' in locals():
            doc.close()
    logging.info(f"Finished extracting text from {pdf_file}.")
    return text

# Function to split text into chunks using LangChain
def split_text_into_chunks(text, chunk_size):
    logging.info(f"Splitting text into chunks of {chunk_size} characters...")
    if not isinstance(text, str):
        logging.error(f"Expected text to be a string but got {type(text)}: {text}")
        return []
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=0)
    try:
        chunks = text_splitter.split_text(text)
        logging.debug(f"First chunk of {text[:50]}...")  # Log first 50 characters of the first chunk
    except Exception as e:
        logging.error(f"Error splitting text: {e}")
        chunks = []
    logging.info(f"Finished splitting text into chunks.")
    return chunks

# Setup the SQLite database and create the table, optionally dropping the existing table if it exists
def setup_database(db_name, reset_db):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    if reset_db:
        cursor.execute('DROP TABLE IF EXISTS pdf_chunks')
        cursor.execute('DROP TABLE IF EXISTS word_frequencies')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pdf_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            chunk_text TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS word_frequencies (
            word TEXT PRIMARY KEY,
            frequency INTEGER
        )
    ''')
    conn.commit()
    conn.close()

# Function to execute a database operation with a retry mechanism
def execute_with_retry(func, *args, retries=999, delay=10, **kwargs):
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if 'locked' in str(e):
                logging.warning(f"{attempt+1}/{retries} Database is locked, retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                raise
    raise Exception(f"Failed to execute after {retries} retries")

# Function to store text chunks in the SQLite database
def store_chunks_in_db(file_name, chunks, db_name):
    def _store():
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        for index, chunk in enumerate(chunks):
            cursor.execute('''
                INSERT INTO pdf_chunks (file_name, chunk_index, chunk_text) VALUES (?, ?, ?)
            ''', (os.path.basename(file_name), index, chunk))
        conn.commit()
        conn.close()
    execute_with_retry(_store)
    logging.info(f"Stored {len(chunks)} chunks for {file_name} in the database.")

# Function to split text into chunks using LangChain
def split_text_into_chunks(text, chunk_size):
    logging.info(f"Splitting text into chunks of {chunk_size} characters...")
    if not isinstance(text, str):
        logging.error(f"Expected text to be a string but got {type(text)}: {text}")
        return []
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=0)
    try:
        chunks = text_splitter.split_text(text)
        # Log first 50 characters of the first chunk
        logging.debug(f"First chunk of {text[:50]}...")
    except Exception as e:
        logging.error(f"Error splitting text: {e}")
        chunks = []
    logging.info(f"Finished splitting text into chunks.")
    return chunks

# Function to extract, split, and store text from a PDF file
def extract_split_and_store_pdf(pdf_file, chunk_size, db_name):
    try:
        text = extract_text_from_pdf(pdf_file)
        if text is None or text == "":
            logging.warning(f"No text extracted from {pdf_file}.")
            return
        logging.debug(f"Extracted text type: {type(text)}, length: {len(text)}")
        chunks = split_text_into_chunks(text, chunk_size=chunk_size)
        if not chunks:
            logging.warning(f"No chunks created for {pdf_file}.")
            return
        store_chunks_in_db(pdf_file, chunks, db_name)
    except Exception as e:
        logging.error(f"Error processing {pdf_file}: {e}")

# Function to process multiple PDF files concurrently
def process_files_in_parallel(pdf_files, reset_db, chunk_size, db_name):
    setup_database(db_name, reset_db)  # Ensure the database is reset before processing files
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_file = {executor.submit(extract_split_and_store_pdf, pdf_file, chunk_size, db_name): pdf_file for pdf_file in pdf_files}
        
        total_files = len(pdf_files)
        completed_files = 0

        for future in concurrent.futures.as_completed(future_to_file):
            pdf_file = future_to_file[future]
            try:
                future.result()
                completed_files += 1
                logging.info(f"Completed {completed_files}/{total_files} files: {pdf_file}")
                print(f"Completed {completed_files}/{total_files} files: {os.path.basename(pdf_file).removesuffix('.pdf')}.")
            except Exception as e:
                logging.error(f"Error processing {pdf_file}: {e}")

# Batch processing for merging chunks and cleaning text
def process_chunks_in_batches(db_name, batch_size=1000):
    # Function to retrieve chunks in batches
    def retrieve_chunks_in_batches():
        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pdf_chunks")
        total_chunks = cursor.fetchone()[0]
        for offset in range(0, total_chunks, batch_size):
            cursor.execute("SELECT chunk_text FROM pdf_chunks ORDER BY id LIMIT ? OFFSET ?", (batch_size, offset))
            yield [row[0] for row in cursor.fetchall()]
        conn.close()

    # Function to merge split words in the chunks
    def merge_split_words(chunks):
        merged_chunks = []
        buffer = ''
        for chunk in chunks:
            if buffer:
                chunk = buffer + chunk
                buffer = ''
            if not chunk[-1].isspace() and not chunk[-1].isalpha():
                buffer = chunk.split()[-1]
                chunk = chunk.rsplit(' ', 1)[0]
            merged_chunks.append(chunk)
        if buffer:
            merged_chunks.append(buffer)
        return merged_chunks

    # Function to clean the text by removing non-alphabetic characters and converting to lowercase
    def clean_text(text):
        text = re.sub(r'[^a-zA-Z\s]', '', text).lower()
        words = text.split()
        return words

    # Dictionary to store word frequencies
    word_frequencies = defaultdict(int)

    # Retrieve and process chunks in batches
    for chunk_batch in retrieve_chunks_in_batches():
        merged_chunks = merge_split_words(chunk_batch)
        for chunk in merged_chunks:
            cleaned_words = clean_text(chunk)
            for word in cleaned_words:
                word_frequencies[word] += 1

    # Store word frequencies in database
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    for word, freq in word_frequencies.items():
        cursor.execute('''
            INSERT INTO word_frequencies (word, frequency) VALUES (?, ?)
            ON CONFLICT(word) DO UPDATE SET frequency = frequency + ?
        ''', (word, freq, freq))
    conn.commit()
    conn.close()

    return word_frequencies

def get_file_list(folder_path: str) -> list[str]:
    """
    Returns a sorted list of file paths for all PDF files in the given folder path.

    Args:
        folder_path (str): The path to the folder containing the PDF files.

    Returns:
        list[str]: A sorted list of file paths for all PDF files in the given folder path.
    """
    return sorted([os.path.join(folder_path, file) 
                        for file in os.listdir(folder_path) 
                        if file.lower().endswith('.pdf')])

def update_database(BOOKS_folder_path:str, DB_name:str, reset_db=True, chunk_size=8000) -> list[tuple[str, int]]:
    """
    Update the database with PDF files from the specified folder path.

    Args:
        BOOKS_folder_path (str): The path to the folder containing the PDF files.
        DB_name (str): The name of the database.
        reset_db (bool, optional): Whether to reset the database before processing. Defaults to True.
        chunk_size (int, optional): The number of files to process in parallel. Defaults to 8000.

    Returns:
        list[tuple[str, int]]: A list of tuples containing the word and its frequency.

    This function updates the database with PDF files from the specified folder path. 
    It first retrieves the list of PDF files from the folder, resets the database if specified, 
    and then processes the files in parallel using the specified chunk size. After processing is complete, 
    it processes the chunks in batches and stores the word frequencies in the database. The function 
    returns the word frequencies as a list of tuples.
    """
    pdf_files = get_file_list(BOOKS_folder_path)
    
    # Reset the database before processing
    setup_database(reset_db=reset_db, db_name=DB_name)
    logging.info(f"Starting processing of {len(pdf_files)} PDF files...")

    print(f"Starting processing of {len(pdf_files)} PDF files...")
    process_files_in_parallel(pdf_files, 
                              reset_db=reset_db, 
                              chunk_size=chunk_size, 
                              db_name=DB_name)
    
    logging.info("Processing complete.")
    print("Processing complete.")
    
    # Now process the chunks in batches and store word frequencies
    logging.info("Starting batch processing of chunks...")

    word_frequencies = process_chunks_in_batches(db_name=DB_name)

    logging.info("Batch processing complete.")
    print("Batch processing complete.")

    return word_frequencies

def retrive_word_frequencies(DB_name: str) -> list[tuple[str, int]]:
    """
    Retrieve the word frequencies from the specified database.

    Args:
        DB_name (str): The name of the database.

    Returns:
        list[tuple[str, int]]: A list of tuples containing the word and its frequency.
    """
    DB_NAME = DB_name
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT word, frequency FROM word_frequencies")
    word_frequencies = cursor.fetchall()
    conn.close()
    return word_frequencies