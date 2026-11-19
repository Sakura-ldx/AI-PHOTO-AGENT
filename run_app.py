from pathlib import Path

from app.env_file import load_env_file
from app.launcher import main


if __name__ == "__main__":
    load_env_file(Path(__file__).with_name(".env"))
    main()
