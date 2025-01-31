"""Helpers for other modules."""

from __future__ import unicode_literals

from dvc.utils.compat import (
    str,
    builtin_str,
    open,
    cast_bytes_py2,
    StringIO,
    fspath_py35,
    fspath,
    makedirs as _makedirs,
)

import os
import sys
import stat
import math
import json
import errno
import shutil
import hashlib
import nanotime
import time
import colorama
import re
import logging

from shortuuid import uuid
from ruamel.yaml import YAML


logger = logging.getLogger(__name__)

LOCAL_CHUNK_SIZE = 1024 * 1024
LARGE_FILE_SIZE = 1024 * 1024 * 1024
LARGE_DIR_SIZE = 100


def dos2unix(data):
    return data.replace(b"\r\n", b"\n")


def file_md5(fname):
    """ get the (md5 hexdigest, md5 digest) of a file """
    from dvc.progress import progress
    from dvc.istextfile import istextfile

    if os.path.exists(fname):
        hash_md5 = hashlib.md5()
        binary = not istextfile(fname)
        size = os.path.getsize(fname)
        bar = False
        if size >= LARGE_FILE_SIZE:
            bar = True
            msg = "Computing md5 for a large file {}. This is only done once."
            logger.info(msg.format(relpath(fname)))
            name = relpath(fname)
            total = 0

        with open(fname, "rb") as fobj:
            while True:
                data = fobj.read(LOCAL_CHUNK_SIZE)
                if not data:
                    break

                if bar:
                    total += len(data)
                    progress.update_target(name, total, size)

                if binary:
                    chunk = data
                else:
                    chunk = dos2unix(data)

                hash_md5.update(chunk)

        if bar:
            progress.finish_target(name)

        return (hash_md5.hexdigest(), hash_md5.digest())

    return (None, None)


def bytes_md5(byts):
    hasher = hashlib.md5()
    hasher.update(byts)
    return hasher.hexdigest()


def dict_filter(d, exclude=()):
    """
    Exclude specified keys from a nested dict
    """

    def fix_key(k):
        return str(k) if isinstance(k, builtin_str) else k

    if isinstance(d, list):
        return [dict_filter(e, exclude) for e in d]

    if isinstance(d, dict):
        items = ((fix_key(k), v) for k, v in d.items())
        return {
            k: dict_filter(v, exclude) for k, v in items if k not in exclude
        }

    return d


def dict_md5(d, exclude=()):
    filtered = dict_filter(d, exclude)
    byts = json.dumps(filtered, sort_keys=True).encode("utf-8")
    return bytes_md5(byts)


def copyfile(src, dest, no_progress_bar=False, name=None):
    """Copy file with progress bar"""
    from dvc.exceptions import DvcException
    from dvc.progress import progress
    from dvc.system import System

    copied = 0
    name = name if name else os.path.basename(dest)
    total = os.stat(src).st_size

    if os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(src))

    try:
        System.reflink(src, dest)
    except DvcException:
        with open(src, "rb") as fsrc, open(dest, "wb+") as fdest:
            while True:
                buf = fsrc.read(LOCAL_CHUNK_SIZE)
                if not buf:
                    break
                fdest.write(buf)
                copied += len(buf)
                if not no_progress_bar:
                    progress.update_target(name, copied, total)

    if not no_progress_bar:
        progress.finish_target(name)


def makedirs(path, exist_ok=False, mode=None):
    path = fspath_py35(path)

    if mode is None:
        _makedirs(path, exist_ok=exist_ok)
        return

    umask = os.umask(0)
    try:
        _makedirs(path, exist_ok=exist_ok, mode=mode)
    finally:
        os.umask(umask)


def move(src, dst, mode=None):
    """Atomically move src to dst and chmod it with mode.

    Moving is performed in two stages to make the whole operation atomic in
    case src and dst are on different filesystems and actual physical copying
    of data is happening.
    """

    src = fspath_py35(src)
    dst = fspath_py35(dst)

    dst = os.path.abspath(dst)
    tmp = "{}.{}".format(dst, str(uuid()))

    if os.path.islink(src):
        shutil.copy(os.readlink(src), tmp)
        os.unlink(src)
    else:
        shutil.move(src, tmp)

    if mode is not None:
        os.chmod(tmp, mode)

    shutil.move(tmp, dst)


def _chmod(func, p, excinfo):
    perm = os.lstat(p).st_mode
    perm |= stat.S_IWRITE

    try:
        os.chmod(p, perm)
    except OSError as exc:
        # broken symlink or file is not owned by us
        if exc.errno not in [errno.ENOENT, errno.EPERM]:
            raise

    func(p)


def remove(path):
    logger.debug("Removing '{}'".format(relpath(path)))

    try:
        if os.path.isdir(path):
            shutil.rmtree(path, onerror=_chmod)
        else:
            _chmod(os.unlink, path, None)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise


def _split(list_to_split, chunk_size):
    return [
        list_to_split[i : i + chunk_size]
        for i in range(0, len(list_to_split), chunk_size)
    ]


def _to_chunks_by_chunks_number(list_to_split, num_chunks):
    chunk_size = int(math.ceil(float(len(list_to_split)) / num_chunks))

    if len(list_to_split) == 1:
        return [list_to_split]

    if chunk_size == 0:
        chunk_size = 1

    return _split(list_to_split, chunk_size)


def to_chunks(list_to_split, num_chunks=None, chunk_size=None):
    if (num_chunks and chunk_size) or (not num_chunks and not chunk_size):
        raise ValueError(
            "One and only one of 'num_chunks', 'chunk_size' must be defined"
        )
    if chunk_size:
        return _split(list_to_split, chunk_size)
    return _to_chunks_by_chunks_number(list_to_split, num_chunks)


# NOTE: Check if we are in a bundle
# https://pythonhosted.org/PyInstaller/runtime-information.html
def is_binary():
    return getattr(sys, "frozen", False)


# NOTE: Fix env variables modified by PyInstaller
# http://pyinstaller.readthedocs.io/en/stable/runtime-information.html
def fix_env(env=None):
    if env is None:
        env = os.environ.copy()
    else:
        env = env.copy()

    if is_binary():
        lp_key = "LD_LIBRARY_PATH"
        lp_orig = env.get(lp_key + "_ORIG", None)
        if lp_orig is not None:
            # NOTE: py2 doesn't like unicode strings in environ
            env[cast_bytes_py2(lp_key)] = cast_bytes_py2(lp_orig)
        else:
            env.pop(lp_key, None)

    return env


def convert_to_unicode(data):
    if isinstance(data, builtin_str):
        return str(data)
    if isinstance(data, dict):
        return dict(map(convert_to_unicode, data.items()))
    if isinstance(data, (list, tuple)):
        return type(data)(map(convert_to_unicode, data))
    return data


def tmp_fname(fname):
    """ Temporary name for a partial download """
    return fspath(fname) + "." + str(uuid()) + ".tmp"


def current_timestamp():
    return int(nanotime.timestamp(time.time()))


def from_yaml_string(s):
    return YAML().load(StringIO(s))


def to_yaml_string(data):
    stream = StringIO()
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.dump(data, stream)
    return stream.getvalue()


def dvc_walk(top, dvcignore, topdown=True, onerror=None, followlinks=False):
    """
    Proxy for `os.walk` directory tree generator.
    Utilizes DvcIgnoreFilter functionality.
    """
    top = fspath_py35(top)

    for root, dirs, files in os.walk(
        top, topdown=topdown, onerror=onerror, followlinks=followlinks
    ):

        if dvcignore:
            dirs[:], files[:] = dvcignore(root, dirs, files)

        yield root, dirs, files


def walk_files(directory, dvcignore):
    for root, _, files in dvc_walk(directory, dvcignore):
        for f in files:
            yield os.path.join(root, f)


def colorize(message, color=None):
    """Returns a message in a specified color."""
    if not color:
        return message

    colors = {
        "green": colorama.Fore.GREEN,
        "yellow": colorama.Fore.YELLOW,
        "blue": colorama.Fore.BLUE,
        "red": colorama.Fore.RED,
    }

    return "{color}{message}{nc}".format(
        color=colors.get(color, ""), message=message, nc=colorama.Fore.RESET
    )


def boxify(message, border_color=None):
    """Put a message inside a box.

    Args:
        message (unicode): message to decorate.
        border_color (unicode): name of the color to outline the box with.
    """
    lines = message.split("\n")
    max_width = max(_visual_width(line) for line in lines)

    padding_horizontal = 5
    padding_vertical = 1

    box_size_horizontal = max_width + (padding_horizontal * 2)

    chars = {"corner": "+", "horizontal": "-", "vertical": "|", "empty": " "}

    margin = "{corner}{line}{corner}\n".format(
        corner=chars["corner"], line=chars["horizontal"] * box_size_horizontal
    )

    padding_lines = [
        "{border}{space}{border}\n".format(
            border=colorize(chars["vertical"], color=border_color),
            space=chars["empty"] * box_size_horizontal,
        )
        * padding_vertical
    ]

    content_lines = [
        "{border}{space}{content}{space}{border}\n".format(
            border=colorize(chars["vertical"], color=border_color),
            space=chars["empty"] * padding_horizontal,
            content=_visual_center(line, max_width),
        )
        for line in lines
    ]

    box_str = "{margin}{padding}{content}{padding}{margin}".format(
        margin=colorize(margin, color=border_color),
        padding="".join(padding_lines),
        content="".join(content_lines),
    )

    return box_str


def _visual_width(line):
    """Get the the number of columns required to display a string"""

    return len(re.sub(colorama.ansitowin32.AnsiToWin32.ANSI_CSI_RE, "", line))


def _visual_center(line, width):
    """Center align string according to it's visual width"""

    spaces = max(width - _visual_width(line), 0)
    left_padding = int(spaces / 2)
    right_padding = spaces - left_padding

    return (left_padding * " ") + line + (right_padding * " ")


def relpath(path, start=os.curdir):
    path = fspath(path)
    start = os.path.abspath(fspath(start))

    # Windows path on different drive than curdir doesn't have relpath
    if os.name == "nt" and not os.path.commonprefix(
        [start, os.path.abspath(path)]
    ):
        return path
    return os.path.relpath(path, start)
