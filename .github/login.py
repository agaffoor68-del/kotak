import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import login


if __name__ == "__main__":
    login()
    print("Kotak Neo login successful.")
