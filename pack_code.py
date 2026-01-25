import os
import json

# Configuration
OUTPUT_FILENAME = "code.txt"
SENSITIVE_KEYS = {"password", "ap_password", "secret", "token", "api_key"}
IGNORE_DIRS = {".git", ".vscode", "__pycache__", "venv", "env", ".idea"}

def redact_data(data):
    """Recursively redact sensitive keys in a dictionary or list."""
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in SENSITIVE_KEYS:
                data[k] = "***REDACTED***"
            else:
                redact_data(v)
    elif isinstance(data, list):
        for item in data:
            redact_data(item)
    return data

def process_files():
    # Get the directory where this script is located
    base_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(base_dir, OUTPUT_FILENAME)
    
    print(f"Scanning directory: {base_dir}")
    
    with open(output_path, "w", encoding="utf-8") as outfile:
        for root, dirs, files in os.walk(base_dir):
            # Modify dirs in-place to skip ignored directories
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            
            for file in files:
                if file.endswith((".py", ".json")):
                    # Skip this script and the output file itself
                    if file == os.path.basename(__file__) or file == OUTPUT_FILENAME:
                        continue

                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, base_dir).replace("\\", "/")
                    
                    header = f"./{rel_path} :"
                    print(f"Processing {header}")
                    outfile.write(f"{header}\n")
                    
                    try:
                        if file.endswith(".json"):
                            with open(full_path, "r", encoding="utf-8") as f:
                                try:
                                    content_data = json.load(f)
                                    redacted = redact_data(content_data)
                                    content = json.dumps(redacted, indent=2)
                                except json.JSONDecodeError:
                                    # If JSON is invalid, read as raw text
                                    f.seek(0)
                                    content = f.read()
                        else:
                            with open(full_path, "r", encoding="utf-8") as f:
                                content = f.read()
                        
                        outfile.write(content)
                        outfile.write("\n\n")
                    except Exception as e:
                        outfile.write(f"# Error reading file: {e}\n\n")

    print(f"✅ Successfully created {OUTPUT_FILENAME}")

if __name__ == "__main__":
    process_files()