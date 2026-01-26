import os
import re

INPUT_FILENAME = "code.txt"
IGNORE_FILES = {'pack_code.py', 'unpack_code.py'}

def unpack_files():
    if not os.path.exists(INPUT_FILENAME):
        print(f"❌ {INPUT_FILENAME} not found.")
        return

    print(f"📖 Reading {INPUT_FILENAME}...")
    with open(INPUT_FILENAME, "r", encoding="utf-8") as f:
        content = f.read()

    # Split by header: start of line, ./path/to/file, space, colon, end of line
    # (?m) enables multiline mode so ^ matches start of line
    parts = re.split(r'(?m)^(\./.*) :$', content)
    
    # parts[0] is preamble (usually empty)
    # parts[1] is filename, parts[2] is content
    # parts[3] is filename, parts[4] is content...
    
    files_updated = 0
    
    for i in range(1, len(parts), 2):
        header_path = parts[i].strip()
        file_content = parts[i+1]
        
        if os.path.basename(header_path) in IGNORE_FILES:
            print(f"⏭️  Skipping ignored file: {header_path}")
            continue
        
        # Only process .py files to avoid overwriting configs with redacted JSON
        if not header_path.endswith(".py"):
            print(f"⏭️  Skipping non-python file: {header_path}")
            continue

        # Clean up content
        # 1. Remove the immediate newline after header (from the split)
        if file_content.startswith("\n"):
            file_content = file_content[1:]
        
        # 2. Remove the trailing newlines added by pack_code separator
        file_content = file_content.rstrip() + "\n"
        
        # Normalize path for current OS (converts / to \ on Windows)
        file_path = os.path.normpath(header_path)
        
        # Ensure directory exists
        dir_name = os.path.dirname(file_path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)
            
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)
            print(f"✅ Updated: {file_path}")
            files_updated += 1
        except Exception as e:
            print(f"❌ Failed to write {file_path}: {e}")

    print(f"🎉 Finished. Updated {files_updated} files.")

if __name__ == "__main__":
    unpack_files()