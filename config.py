import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    LINKEDIN_EMAIL: str = os.getenv("LINKEDIN_EMAIL", "")
    LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    SCROLL_COUNT: int = int(os.getenv("SCROLL_COUNT", "5"))
    DELAY_MIN: float = float(os.getenv("DELAY_MIN", "2"))
    DELAY_MAX: float = float(os.getenv("DELAY_MAX", "5"))
    LOG_DIR: str = os.path.join(os.path.dirname(__file__), "logs")
    STATE_DIR: str = os.path.join(os.path.dirname(__file__), "state")

    # Sort mode: "relevance" (top/engaged posts) or "date_posted" (newest first)
    SORT_BY: str = os.getenv("SORT_BY", "relevance")    

    @classmethod
    def validate(cls):
        if not cls.LINKEDIN_EMAIL or not cls.LINKEDIN_PASSWORD:
            raise ValueError(
                "LINKEDIN_EMAIL and LINKEDIN_PASSWORD must be set in .env file. "
                "Copy .env.example to .env and fill in your credentials."
            )
        os.makedirs(cls.LOG_DIR, exist_ok=True)
        os.makedirs(cls.STATE_DIR, exist_ok=True)

        if cls.SORT_BY not in ("relevance", "date_posted"):
            print(f"[CONFIG] Invalid SORT_BY '{cls.SORT_BY}', defaulting to 'relevance'")
            cls.SORT_BY = "relevance"