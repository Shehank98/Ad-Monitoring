"""
Channel → TC PDF converter dispatcher.

Maps channel name strings (case-insensitive, partial match) to the appropriate
parser module.  Each parser exposes:

    parse_pdf(pdf_path: str) -> pd.DataFrame

where the DataFrame has columns:
    Date, Programme, Aired_Time, TC_Theme, Duration

To add a new channel converter:
1. Create verification/tc_converters/<channel_slug>.py
2. Add an entry to CHANNEL_CONVERTERS below.
"""

from verification.tc_converters import sirasa_tv

# Keys are lowercase substrings of the channel name as stored in the DB.
# The first key that is a substring of the normalised channel name wins.
CHANNEL_CONVERTERS = {
    'sirasa': sirasa_tv,
    # 'rupavahini': rupavahini,   # future
}


def get_converter(channel: str):
    """
    Return the converter module for *channel*, or None if no converter exists.

    Parameters
    ----------
    channel : str
        The channel string from the TC upload form (e.g. "Tv - Sirasa TV").

    Returns
    -------
    module or None
    """
    norm = channel.lower().strip()
    for key, module in CHANNEL_CONVERTERS.items():
        if key in norm:
            return module
    return None
