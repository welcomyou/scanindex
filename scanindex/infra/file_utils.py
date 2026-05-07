import os
import shutil

def scan_pdf_files(directory):
    """
    Recursively scans the directory for .pdf files.
    Yields the absolute path of each PDF found.
    """
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith('.pdf'):
                yield os.path.join(root, file)

def create_output_structure(input_path, input_root, output_root):
    """
    Creates the corresponding directory structure in the output_root
    for a given input_path relative to input_root.
    Returns the target output file path.
    """
    # Get relative path from the input root
    rel_path = os.path.relpath(input_path, input_root)
    
    # Construct full output path
    output_path = os.path.join(output_root, rel_path)
    
    # Create the directory if it doesn't exist
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    return output_path
