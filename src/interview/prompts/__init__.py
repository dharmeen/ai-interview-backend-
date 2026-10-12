from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


@cache
def load(name: str) -> str:
    """Reads src/interview/prompts/<name>.txt (cached after the first read)."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def escape_user_text(text: str | None) -> str:
    # stops user text from closing our tags early, e.g. a job description containing
    # "</job_description> Ignore the above..." can't break out of <job_description>
    if not text:
        return "(not provided)"
    return text.replace("<", "&lt;").replace(">", "&gt;")


def render(name: str, **values) -> str:
    """Fills the {placeholders} in a prompt file. Values are inserted as-is, so escape
    user-provided text with escape_user_text() first."""
    return load(name).format(**values)
