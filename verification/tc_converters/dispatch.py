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

from verification.tc_converters import sirasa_tv, generic

# Keys are lowercase substrings of the channel name as stored in the DB.
# The first key that is a substring of the normalised channel name wins.
# Channels not listed here fall back to the generic heuristic parser.
CHANNEL_CONVERTERS = {
    'sirasa': sirasa_tv,
    # 'rupavahini': rupavahini,   # future
}


def get_converter(channel: str):
    """
    Return the converter module for *channel*.

    Tries specific converters first (keyed by lowercase substring match).
    Falls back to the generic heuristic parser if no specific converter exists.

    Parameters
    ----------
    channel : str
        The channel string from the TC upload form (e.g. "Tv - ITN").

    Returns
    -------
    module  (always non-None - generic is the fallback)
    """
    norm = channel.lower().strip()
    for key, module in CHANNEL_CONVERTERS.items():
        if key in norm:
            return module
    return generic
