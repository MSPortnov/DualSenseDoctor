"""Language of the plugin's messages and reports."""

# ru / en. Set by the interface ("Language" setting), stored in settings.json.
LANG = "en"


def tr(ru, en):
    return ru if LANG == "ru" else en


def current():
    return LANG


def set_lang(lang):
    """Sets the language of messages and reports: "ru" or (anything else) "en". Returns the value set."""
    global LANG
    LANG = "ru" if lang == "ru" else "en"
    return LANG

