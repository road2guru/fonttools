#!/usr/bin/env python
"""
Tools to parse data files from the Unicode Character Database.
"""

from __future__ import print_function, absolute_import, division
from __future__ import unicode_literals
from fontTools.misc.py23 import *

try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen
from contextlib import closing, contextmanager
import re
from codecs import iterdecode
import logging
import os
from os.path import abspath, dirname, join as pjoin, pardir, sep


UNIDATA_URL = "https://unicode.org/Public/UNIDATA/"
UNIDATA_LICENSE_URL = "http://unicode.org/copyright.html#License"

# by default save output files to ../Lib/fontTools/unicodedata/
UNIDATA_PATH = pjoin(abspath(dirname(__file__)), pardir,
                     "Lib", "fontTools", "unicodedata") + sep

SRC_ENCODING = "# -*- coding: utf-8 -*-\n"

NOTICE = "# NOTE: This file was auto-generated with MetaTools/buildUCD.py.\n"

MAX_UNICODE = 0x10FFFF

log = logging.getLogger()


@contextmanager
def open_unidata_file(filename):
    """Open a text file from https://unicode.org/Public/UNIDATA/"""
    url = UNIDATA_URL + filename
    with closing(urlopen(url)) as response:
        yield iterdecode(response, encoding="utf-8")


def parse_unidata_header(infile):
    """Read the top header of data files, until the first line
    that does not start with '#'.
    """
    header = []
    line = next(infile)
    while line.startswith("#"):
        header.append(line)
        line = next(infile)
    return "".join(header)


def parse_range_properties(infile, default=None, is_set=False):
    """Parse a Unicode data file containing a column with one character or
    a range of characters, and another column containing a property value
    separated by a semicolon. Comments after '#' are ignored.

    If the ranges defined in the data file are not continuous, assign the
    'default' property to the unassigned codepoints.

    Return a list of (start, end, property_name) tuples.
    """
    ranges = []
    line_regex = re.compile(
        r"^"
        r"([0-9A-F]{4,6})"  # first character code
        r"(?:\.\.([0-9A-F]{4,6}))?"  # optional second character code
        r"\s*;\s*"
        r"([^#]+)")  # everything up to the potential comment
    for line in infile:
        match = line_regex.match(line)
        if not match:
            continue

        first, last, data = match.groups()
        if last is None:
            last = first

        first = int(first, 16)
        last = int(last, 16)
        data = tostr(data.rstrip(), encoding="ascii")

        ranges.append((first, last, data))

    ranges.sort()

    if isinstance(default, unicode):
        default = tostr(default, encoding="ascii")

    # fill the gaps between explicitly defined ranges
    last_start, last_end = -1, -1
    full_ranges = []
    for start, end, value in ranges:
        assert last_end < start
        assert start <= end
        if start - last_end > 1:
            full_ranges.append((last_end+1, start-1, default))
        if is_set:
            value = set(value.split())
        full_ranges.append((start, end, value))
        last_start, last_end = start, end
    if last_end != MAX_UNICODE:
        full_ranges.append((last_end+1, MAX_UNICODE, default))

    # reduce total number of ranges by combining continuous ones
    last_start, last_end, last_value = full_ranges.pop(0)
    merged_ranges = []
    for start, end, value in full_ranges:
        if value == last_value:
            continue
        else:
            merged_ranges.append((last_start, start-1, last_value))
            last_start, line_end, last_value = start, end, value
    merged_ranges.append((last_start, MAX_UNICODE, last_value))

    # make sure that the ranges cover the full unicode repertoire
    assert merged_ranges[0][0] == 0
    for (cs, ce, cv), (ns, ne, nv) in zip(merged_ranges, merged_ranges[1:]):
        assert ce+1 == ns
    assert merged_ranges[-1][1] == MAX_UNICODE

    return merged_ranges


def _set_repr(value):
    return 'None' if value is None else "{{{}}}".format(
        ", ".join(repr(v) for v in sorted(value)))


def build_ranges(filename, local_ucd=None, output_path=None,
                 default=None, is_set=False):
    """Fetch 'filename' UCD data file from Unicode official website, parse
    the ranges and properties and write them as two Python lists
    to 'fontTools.unicodedata.<filename>.py'.

    To load the data file from a local directory, you can use the
    'local_ucd' argument.
    """
    modname = os.path.splitext(filename)[0] + ".py"
    if not output_path:
        output_path = UNIDATA_PATH + modname

    if local_ucd:
        log.info("loading '%s' from local directory '%s'", filename, local_ucd)
        cm = open(pjoin(local_ucd, filename), "r", encoding="utf-8")
    else:
        log.info("downloading '%s' from '%s'", filename, UNIDATA_URL)
        cm = open_unidata_file(filename)

    with cm as f:
        header = parse_unidata_header(f)
        ranges = parse_range_properties(f, default=default, is_set=is_set)

    max_value_length = min(56, max(len(repr(v)) for _, _, v in ranges))

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(SRC_ENCODING)
        f.write("#\n")
        f.write(NOTICE)
        f.write("# Source: {}{}\n".format(UNIDATA_URL, filename))
        f.write("# License: {}\n".format(UNIDATA_LICENSE_URL))
        f.write("#\n")
        f.write(header+"\n\n")

        f.write("RANGES = [\n")
        for first, last, value in ranges:
            f.write("    0x{:0>4X},  # .. 0x{:0>4X} ; {}\n".format(
                first, last, _set_repr(value) if is_set else value))
        f.write("]\n")

        f.write("\n")
        f.write("VALUES = [\n")
        for first, last, value in ranges:
            if is_set:
                value_repr = "{},".format(_set_repr(value))
            else:
                value_repr = "{!r},".format(value)
            f.write("    {}  # {:0>4X}..{:0>4X}\n".format(
                value_repr.ljust(max_value_length+1), first, last))
        f.write("]\n")

    log.info("saved new file: '%s'", os.path.normpath(output_path))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate fontTools.unicodedata from UCD data files")
    parser.add_argument(
        '--ucd-path', help="Path to local folder containing UCD data files")
    parser.add_argument('-q', '--quiet', action="store_true")
    options = parser.parse_args()

    level = "WARNING" if options.quiet else "INFO"
    logging.basicConfig(level=level, format="%(message)s")

    build_ranges("Blocks.txt", local_ucd=options.ucd_path, default="No_Block")
    build_ranges("Scripts.txt", local_ucd=options.ucd_path, default="Unknown")
    build_ranges("ScriptExtensions.txt", local_ucd=options.ucd_path,
                 is_set=True)


if __name__ == "__main__":
    import sys
    sys.exit(main())
