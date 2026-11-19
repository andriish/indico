# This file is part of Indico.
# Copyright (C) 2002 - 2026 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import os
from importlib.resources import as_file, files
from xml.sax import saxutils  # noqa: S406

from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from indico.core.logger import Logger
from indico.util.string import sanitize_for_platypus


def _is_string_html(s):
    # yeah, this function is pretty ugly. it's legacy and should eventually be replaced :)
    if not isinstance(s, str):
        return False
    s = s.lower()
    return any(tag in s for tag in ('<p>', '<p ', '<br', '<li>'))


def escape(text):
    if text is None:
        return ''
    if _is_string_html(text):
        return sanitize_for_platypus(text)
    else:
        return saxutils.escape(text)


FONT_PATH_ENV_VAR = 'INDICO_FONT_PATH'


def get_font_dirs():
    """Get the directories in which PDF fonts are looked up.

    The ``indico_fonts`` package (if installed) comes first, followed by the
    directories listed in the ``INDICO_FONT_PATH`` environment variable
    (separated like ``PATH``, i.e. ``:`` on Linux).
    """
    dirs = []
    try:
        with as_file(files('indico_fonts')) as font_dir:
            dirs.append(os.fspath(font_dir))
    except ImportError:
        pass
    for path in os.environ.get(FONT_PATH_ENV_VAR, '').split(os.pathsep):
        if path and os.path.isdir(path) and path not in dirs:
            dirs.append(path)
    return dirs


def find_font_files():
    """Get a dict mapping font filenames to their absolute paths.

    If a font exists in multiple directories, the first one wins.
    """
    found = {}
    for font_dir in get_font_dirs():
        for entry in os.scandir(font_dir):
            if entry.is_file():
                found.setdefault(entry.name, os.path.abspath(entry.path))
    return found


_REPORTLAB_FONT_FAMILIES = {
    'Times-Roman': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-Bold-Italic'),
    'Sans': ('Sans', 'Sans-Bold', 'Sans-Italic', 'Sans-Bold-Italic'),
    'Courier': ('Courier', 'Courier-Bold', 'Courier-Italic', 'Courier-Bold-Italic'),
    'LinuxLibertine': ('LinuxLibertine', 'LinuxLibertine-Bold', 'LinuxLibertine-Italic', 'LinuxLibertine-Bold-Italic'),
}

_REPORTLAB_FONT_FILES = {
    'Times-Roman': 'LiberationSerif-Regular.ttf',
    'Times-Bold': 'LiberationSerif-Bold.ttf',
    'Times-Italic': 'LiberationSerif-Italic.ttf',
    'Times-Bold-Italic': 'LiberationSerif-BoldItalic.ttf',
    'Sans': 'LiberationSans-Regular.ttf',
    'Sans-Bold': 'LiberationSans-Bold.ttf',
    'Sans-Italic': 'LiberationSans-Italic.ttf',
    'Sans-Bold-Italic': 'LiberationSans-BoldItalic.ttf',
    'Courier': 'LiberationMono-Regular.ttf',
    'Courier-Bold': 'LiberationMono-Bold.ttf',
    'Courier-Italic': 'LiberationMono-Italic.ttf',
    'Courier-Bold-Italic': 'LiberationMono-BoldItalic.ttf',
    'LinuxLibertine': 'LinLibertine_Rah.ttf',
    'LinuxLibertine-Bold': 'LinLibertine_RBah.ttf',
    'LinuxLibertine-Italic': 'LinLibertine_RIah.ttf',
    'LinuxLibertine-Bold-Italic': 'LinLibertine_RBIah.ttf',
    'Kochi-Mincho': 'kochi-mincho-subst.ttf',
    'Kochi-Gothic': 'kochi-gothic-subst.ttf',
}

# Built-in (non-embedded) fonts used for custom fonts that cannot be found
_REPORTLAB_FALLBACKS = {
    'Times-Bold-Italic': 'Times-BoldItalic',
    'Courier-Bold-Italic': 'Courier-BoldOblique',
    'Sans': 'Helvetica',
    'Sans-Bold': 'Helvetica-Bold',
    'Sans-Italic': 'Helvetica-Oblique',
    'Sans-Bold-Italic': 'Helvetica-BoldOblique',
    'LinuxLibertine': 'Times-Roman',
    'LinuxLibertine-Bold': 'Times-Bold',
    'LinuxLibertine-Italic': 'Times-Italic',
    'LinuxLibertine-Bold-Italic': 'Times-BoldItalic',
    'Kochi-Mincho': 'Times-Roman',
    'Kochi-Gothic': 'Helvetica',
}

_reportlab_fonts_initialized = False


def init_reportlab_fonts():
    """Register the custom ReportLab fonts.

    Fonts which cannot be found are skipped; ReportLab's built-in fonts
    are used instead for those.
    """
    global _reportlab_fonts_initialized  # noqa: PLW0603
    if _reportlab_fonts_initialized:
        return
    _reportlab_fonts_initialized = True
    available = find_font_files()
    registered = set()
    for name, filename in _REPORTLAB_FONT_FILES.items():
        if filename not in available:
            Logger.get('pdf').warning('Font file %s not found (searched indico_fonts and $%s)',
                                      filename, FONT_PATH_ENV_VAR)
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, available[filename]))
        except Exception:
            Logger.get('pdf').exception('Could not load font file %s', available[filename])
        else:
            registered.add(name)
    for name, fallback in _REPORTLAB_FALLBACKS.items():
        if name not in registered and name not in pdfmetrics.getRegisteredFontNames():
            # keeps e.g. the designer working (it refers to fonts by name), albeit without
            # the real glyphs (so no CJK, for example)
            pdfmetrics.registerFont(pdfmetrics.Font(name, fallback, 'WinAnsiEncoding'))
            registered.add(name)
    for family, (regular, bold, italic, bold_italic) in _REPORTLAB_FONT_FAMILIES.items():
        # only map the variants that exist, using the regular one as the fallback for the others
        if regular not in registered:
            continue
        addMapping(family, 0, 0, regular)
        addMapping(family, 1, 0, bold if bold in registered else regular)
        addMapping(family, 0, 1, italic if italic in registered else regular)
        addMapping(family, 1, 1, bold_italic if bold_italic in registered else regular)
