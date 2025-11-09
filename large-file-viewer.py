import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import os

class VirtualText(tk.Text):
    """Custom Text widget with virtual scrolling."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_path = None
        self.chunk_size = 1024 * 1024  # 1 MB
        self.current_offset = 0
        self.file_size = 0
        self.lock = threading.Lock()

    def load_file(self, file_path):
        """Load the file and display the first chunk."""
        self.file_path = file_path
        self.current_offset = 0
        self.delete(1.0, tk.END)

        try:
            self.file_size = os.path.getsize(file_path)
            self._load_next_chunk()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load file: {e}")

    def _load_next_chunk(self):
        """Load the next chunk of the file in a separate thread."""
        def load():
            with self.lock:
                try:
                    with open(self.file_path, 'r', encoding='utf-8', errors='ignore') as file:
                        file.seek(self.current_offset)
                        chunk = file.read(self.chunk_size)
                        if chunk:
                            self.current_offset += len(chunk)
                            self.insert(tk.END, chunk)
                            self.see(tk.END)
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to read file: {e}")

        threading.Thread(target=load, daemon=True).start()


def launch_viewer(initial_file: str | None = None):
    """Start the Large File Viewer GUI. Optionally open an initial file."""
    # Create the main Tkinter window
    root = tk.Tk()
    root.title("Large File Viewer")

    # Set the application icon
    try:
        root.iconbitmap("text.ico")
    except tk.TclError:
        print("WARNING: Icon file not found or unsupported on this platform.")

    # Create a Text widget with a vertical scrollbar
    frame = tk.Frame(root)
    frame.pack(fill=tk.BOTH, expand=True)

    scrollbar = tk.Scrollbar(frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    text_widget = VirtualText(frame, wrap=tk.NONE, yscrollcommand=scrollbar.set)
    text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar.config(command=text_widget.yview)

    # Create a menu
    menu = tk.Menu(root)
    root.config(menu=menu)

    def open_file():
        """Open a file dialog and load the file into the VirtualText widget."""
        file_path = filedialog.askopenfilename(
            title="Select a Text File",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if file_path:
            text_widget.load_file(file_path)

    file_menu = tk.Menu(menu, tearoff=0)
    menu.add_cascade(label="File", menu=file_menu)
    file_menu.add_command(label="Open", command=open_file)
    file_menu.add_separator()
    file_menu.add_command(label="Exit", command=root.quit)

    # Add a status bar to display cursor position, selection, and encoding
    status_bar = tk.Label(root, text="Row: 1, Column: 1 | Selection: None | Encoding: UTF-8", anchor=tk.W)
    status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def update_status_bar(event=None):
        """Update the status bar with cursor position, selection, and encoding."""
        try:
            # Get cursor position
            cursor_index = text_widget.index(tk.INSERT)
            row, col = map(int, cursor_index.split('.'))

            # Get selection range
            try:
                sel_start = text_widget.index(tk.SEL_FIRST)
                sel_end = text_widget.index(tk.SEL_LAST)
                start_idx = text_widget.index(sel_start).split('.')
                end_idx = text_widget.index(sel_end).split('.')
                start_offset = int(start_idx[1]) + int(start_idx[0]) * 1000  # Approximation
                end_offset = int(end_idx[1]) + int(end_idx[0]) * 1000
                selection_length = end_offset - start_offset
                selection = f"{sel_start} - {sel_end} ({selection_length} chars)"
            except tk.TclError:
                selection = "None"

            # Update status bar text
            status_bar.config(text=f"Row: {row}, Column: {col} | Selection: {selection} | Encoding: UTF-8")
        except Exception as e:
            status_bar.config(text=f"Error updating status: {e}")

    # Bind events to update the status bar
    text_widget.bind("<KeyRelease>", update_status_bar)
    text_widget.bind("<ButtonRelease>", update_status_bar)

    # Optionally open an initial file
    if initial_file and os.path.isfile(initial_file):
        try:
            text_widget.load_file(initial_file)
        except Exception as e:
            messagebox.showerror("Error", f"Unable to open initial file: {e}")

    # Run the Tkinter event loop
    root.geometry("800x600")
    root.mainloop()


if __name__ == "__main__":
    launch_viewer()
