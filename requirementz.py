#!/usr/bin/env python3
# -*- coding: utf-8 -*-

""" requirementz.py
    Check requirements.txt against installed packages using pip and
    requirements-parser.
    Bonus features:
    Check for duplicate entries, search for entries using regex,
    add requirement lines,
    and list requirements or all installed packages (both formatted
    for readability).

    This is for Python 3 only.
    -Christopher Welborn 07-19-2015
"""

# TODO: Figure out what to do with cvs or local requirements. -Cj
import json
import os
import pip
import re
import shutil
import sys
import traceback
from collections import UserList
from functools import total_ordering
from pkg_resources import parse_version
from urllib.error import HTTPError
from urllib.request import urlopen

from requirements.requirement import Requirement
from colr import (
    auto_disable as colr_auto_disable,
    disable as colr_disable,
    docopt,
    Colr as C
)
from fmtblock import FormatBlock
from printdebug import DebugColrPrinter
debugprinter = DebugColrPrinter()
debugprinter.disable()
debug = debugprinter.debug

colr_auto_disable()


NAME = 'Requirementz'
VERSION = '0.2.3'
VERSIONSTR = '{} v. {}'.format(NAME, VERSION)
SCRIPT = os.path.split(os.path.abspath(sys.argv[0]))[1]

USAGESTR = """{versionstr}
    Requirementz checks a requirements.txt package list against installed
    packages or packages found on pypi. It can also show pypi's latest
    information for a package.

    Usage:
        {script} (-h | -v) [-D] [-n]
        {script} [-c | -C | -e] [-L | -r] [-f file] [-D] [-n]
        {script} [-d | -l | -a line... | (-s pat [-i])] [-f file] [-D] [-n]
        {script} -p [-L] [-D] [-n]
        {script} (-P | -S) [-f file] [-D] [-n]
        {script} PACKAGE... [-D] [-n]

    Options:
        PACKAGE              : Show pypi info for package names.
        -a line,--add line   : Add a requirement line to the file.
                               The -a flag can be used multiple times.
        -C,--checklatest     : Check installed packages and latest versions
                               from PyPi against requirements.
        -c,--check           : Check installed packages against requirements.
        -D,--debug           : Print some debug info while running.
        -d,--duplicates      : List any duplicate entries.
        -e,--errors          : Like -c, but only show packages with errors.
        -f file,--file file  : Requirements file to parse.
                               Default: ./requirements.txt
        -h,--help            : Show this help message.
        -i,--ignorecase      : Case insensitive when searching.
        -L,--location        : When listing, sort by location instead of name.
                               When checking, show the package location.
        -l,--list            : List all requirements.
        -n,--nocolor         : Force plain text, with no color codes.
        -P,--pypi            : Show pypi info for all packages in
                               requirements.txt.
        -p,--packages        : List all installed packages.
        -r,--requirement     : Print name and version requirement only for -c.
                               Useful for use with -e, to get a list of
                               packages to install or upgrade.
        -S,--sort            : Sort the requirements file by package name.
        -s pat,--search pat  : Search requirements for text/regex pattern.
        -v,--version         : Show version.

    The default action is to check requirements against installed packages.
    This must be ran with the same interpreter the target `pip` uses,
    which is `pip{py_ver.major}` by default.

    Currently using pip v. {pip_ver} for Python {py_ver.major}.{py_ver.minor}.
""".format(
    script=SCRIPT,
    versionstr=VERSIONSTR,
    pip_ver=pip.__version__,
    py_ver=sys.version_info
)

# Handling this flag the old way for early access (before docopt arg parsing).
DEBUG = ('-D' in sys.argv) or ('--debug' in sys.argv)
if DEBUG:
    debugprinter.enable()

# Map from comparison operator to actual version comparison function.
OP_FUNCS = {
    '==': lambda v1, v2: parse_version(v1) == parse_version(v2),
    '>=': lambda v1, v2: parse_version(v1) >= parse_version(v2),
    '<=': lambda v1, v2: parse_version(v1) <= parse_version(v2),
    '>': lambda v1, v2: parse_version(v1) > parse_version(v2),
    '<': lambda v1, v2: parse_version(v1) < parse_version(v2)
}

# Known EnvironmentError errno's, for better error messages.
FILE_NOT_FOUND = 2
INVALID_PERMISSIONS = 13
FILE_ERRS = {
    # Py2 does not have FileNotFoundError.
    FILE_NOT_FOUND: 'Requirements file not found: {filename}',
    # PermissionsError.
    INVALID_PERMISSIONS: 'Invalid permissions for file: {filename}',
    # Other EnvironmentError.
    None: '{msg}: {filename}\n{exc}'
}

# Operates on ./requirements.txt by default.
DEFAULT_FILE = 'requirements.txt'


def main(argd):
    """ Main entry point, expects doctopt arg dict as argd. """
    global DEBUG
    DEBUG = argd['--debug']
    if argd['--nocolor']:
        colr_disable()

    filename = argd['--file'] or os.path.join(os.getcwd(), DEFAULT_FILE)

    # May opt-in to create a file that doesn't exist.
    if argd['--add']:
        return add_lines(filename, argd['--add'])

    # File must exist for all other flags.
    if argd['--duplicates']:
        return list_duplicates(filename)
    elif argd['--list']:
        return list_requirements(filename)
    elif argd['--packages']:
        return list_packages(location=argd['--location'])
    elif argd['--search']:
        return search_requirements(
            argd['--search'],
            filename=filename,
            ignorecase=argd['--ignorecase']
        )
    elif argd['--check'] or argd['--errors'] or argd['--checklatest']:
        # Explicit check.
        return check_requirements(
            filename,
            errors_only=argd['--errors'],
            spec_only=argd['--requirement'],
            latest=argd['--checklatest'],
            location=argd['--location'],
        )
    elif argd['--pypi']:
        return show_package_infos(get_requirement_names(filename))
    elif argd['--sort']:
        sort_requirements(filename)
        print('Sorted requirements file: {}'.format(filename))
        return 0
    elif argd['PACKAGE']:
        return show_package_infos(argd['PACKAGE'])

    # Default action, check.
    return check_requirements(
        filename,
        errors_only=argd['--errors'],
        spec_only=argd['--requirement'],
        latest=argd['--checklatest'],
    )


def add_lines(filename, lines):
    """ Add a requirements line to the file.
        Returns 0 on success, and 1 on error.
        Prints any errors that occur.
    """
    if not file_ensure_exists(filename):
        return 1
    reqs = Requirementz.from_file(filename)
    msgs = []
    for line in lines:
        try:
            req = RequirementPlus.parse(line)
        except ValueError as ex:
            print_err('Invalid requirement spec.: {}'.format(line))
            return 1

        try:
            if reqs.add_line(line):
                msg = 'Added requirement: {}'.format(req)
            else:
                msg = 'Replaced requirement with: {}'.format(req)
        except ValueError as ex:
            raise FatalError(str(ex))
        else:
            msgs.append(msg)

    try:
        reqs.write(filename=filename)
    except EnvironmentError as ex:
        raise FatalError(
            format_env_err(
                filename=filename,
                exc=ex,
                msg='Failed to write file'
            )
        )
    print('\n'.join(msgs))
    return 0


def check_requirements(
        filename=DEFAULT_FILE,
        errors_only=False, spec_only=False, latest=False, location=False):
    """ Check requirements against installed versions and print status lines
        for all of them.
    """
    reqs = Requirementz.from_file(filename=filename)
    if len(reqs) == 0:
        print('Requirements file is empty.')
        return 1
    errs = 0
    for r in reqs:
        statusline = StatusLine(r)
        if errors_only and not statusline.error:
            continue
        if statusline.error:
            errs += 1
        if spec_only:
            print(statusline.req.to_str(color=True, align=True))
        elif latest:
            print(statusline.with_latest(color=True, location=location))
        else:
            print(statusline.to_str(color=True, location=location))
    return errs


def file_ensure_exists(filename):
    """ Confirm that a requirements.txt exists, create one if the USAGESTR
        wants to. If none exists, and the user does not want to create one,
        return False.
        Returns True on success.
    """
    if os.path.isfile(filename):
        debug('File exists: {}'.format(filename))
        return True

    print('\nThis file doesn\'t exist yet: {}'.format(filename))
    ans = input('Would you like to create it? (y/N): ').strip().lower()
    if not ans.startswith('y'):
        print('\nUser cancelled.')
        return False

    try:
        with open(filename, 'w'):
            pass
        debug('Created an empty {}'.format(filename))
    except EnvironmentError as ex:
        print('\nError creating file: {}\n{}'.format(filename, ex))
        return False
    return True


def format_env_err(**kwargs):
    """ Format a custom message for EnvironmentErrors. """
    exc = kwargs.get('exc', None)
    if exc is None:
        raise ValueError('`exc` is a required kwarg.')
    filename = kwargs.get('filename', getattr(exc, 'filename', ''))
    msg = kwargs.get('msg', 'Error with file')
    return '\n{}'.format(
        FILE_ERRS.get(getattr(exc, 'errno', None)).format(
            filename=filename,
            exc=exc,
            msg=msg
        )
    )


def get_pypi_info(packagename):
    url = 'https://pypi.python.org/pypi/{}/json'.format(packagename)
    try:
        con = urlopen(url)
    except HTTPError as excon:
        excon.msg = '\n'.join((
            'Unable to connect to get info for: {}'.format(url),
            excon.msg
        ))
        raise excon
    else:
        try:
            jsonstr = con.read().decode()
        except UnicodeDecodeError as exdec:
            raise UnicodeDecodeError(
                'Unable to decode data from package info: {}\n{}'.format(
                    url,
                    exdec
                ),
            ) from exdec
        finally:
            con.close()

    try:
        data = json.loads(jsonstr)
    except ValueError as exjson:
        raise ValueError(
            'Unable to decode JSON data from: {}\n{}'.format(url, exjson),
        ) from exjson
    return data


def get_pypi_release_dls(releases):
    """ Count downloads from the `releases` key of pypi info from
        `get_pypi_info`.
        Arguments:
            releases : A dict of release versions and info from
                       get_pypi_info(pkgname)['releases'].
    """
    if not releases:
        return 0
    return sum(
        verinfo.get('downloads', 0)
        for ver in releases
        for verinfo in releases[ver]
    )


def get_requirement_names(filename=DEFAULT_FILE):
    """ Return an iterable of requirement names from a requirements.txt. """
    reqs = Requirementz.from_file(filename=filename)
    return sorted(r.name for r in reqs)


def list_duplicates(filename=DEFAULT_FILE):
    """ Print any duplicate package names found in the file.
        Returns the number of duplicates found.
    """
    dupes = Requirementz.from_file(filename=filename).duplicates()
    dupelen = len(dupes)
    if dupelen == 0:
        print('No duplicate requirements found.')
        return 0

    print('Found {} {} with duplicate entries:'.format(
        dupelen,
        'requirement' if dupelen == 1 else 'requirements'
    ))
    for req, dupcount in dupes.items():
        print('{name:>30} has {num} {plural}'.format(
            name=C(req.name, 'blue'),
            num=C(dupcount, 'blue', style='bright'),
            plural='duplicate' if dupcount == 1 else 'duplicates'
        ))
    return sum(dupes.values())


def list_packages(location=False):
    """ List all installed packages. """
    # Sort by name first.
    pkgs = sorted(PKGS)
    if location:
        # Sort by location, but the name sort is kept.
        pkgs = sorted(pkgs, key=lambda p: PKGS[p].location)
    for pname in pkgs:
        p = PKGS[pname]
        print('{:<30} v. {:<12} {}'.format(
            C(p.project_name, fore='blue'),
            C(pkg_installed_version(pname), fore='cyan'),
            C(p.location, fore='green'),
        ))


def list_requirements(filename=DEFAULT_FILE):
    """ Lists current requirements. """
    reqs = Requirementz.from_file(filename=filename)
    print(
        '\n'.join(
            r.to_str(color=True, align=True)
            for r in sorted(reqs)
        )
    )


def load_packages(local_only=False):
    """ Load all known packages from pip.
        Returns a dict of {package_name.lower(): Package}
        Possibly raises a FatalError.
    """
    debug('Loading package list...')
    try:
        # Map from package name to pip package.
        pkgs = {
            p.project_name.lower(): p
            for p in pip.get_installed_distributions(local_only=local_only)
        }
    except Exception as ex:
        raise FatalError(
            'Unable to retrieve packages with pip: {}'.format(ex)
        )
    debug('Packages loaded: {}'.format(len(pkgs)))
    return pkgs


def pkg_installed_version(pkgname):
    """ Use pip to get an installed package version.
        Return the installed version string, or None if it isn't installed.
    """
    p = PKGS.get(pkgname.lower(), None)
    if p is None:
        return None
    try:
        return p.parsed_version.base_version
    except AttributeError:
        # Python 2...
        vers = []
        for piece in p.parsed_version:
            try:
                vers.append(str(int(piece)))
            except ValueError:
                # final, beta, etc.
                pass
        return '.'.join(vers)


def print_err(*args, **kwargs):
    """ Print a message to stderr by default. """
    if kwargs.get('file', None) is None:
        kwargs['file'] = sys.stderr
    print(
        C(kwargs.get('sep', ' ').join(str(a) for a in args), 'red'),
        **kwargs
    )


def search_requirements(
        pattern, filename=DEFAULT_FILE, ignorecase=True):
    """ Search requirements lines for a text/regex pattern, and print
        results as they are found.
        Returns the number of results found.
    """
    found = 0
    reqs = Requirementz.from_file(filename=filename)
    try:
        for req in reqs.search(pattern, ignorecase=ignorecase):
            found += 1
            print(req)
    except re.error as ex:
        print('\nInvalid regex pattern: {}\n{}'.format(pattern, ex))
        return 1

    print('\nFound {} {}.'.format(
        found,
        'entry' if found == 1 else 'entries'))
    return 0 if found > 0 else 1


def show_package_info(packagename):
    """ Show local and pypi info for a package, by name.
        Returns 0 on success, 1 on failure.
    """
    try:
        pypiinfo = get_pypi_info(packagename)
    except (HTTPError, UnicodeDecodeError, ValueError) as ex:
        print_err(
            'Failed to get pypi info for: {}\n{}'.format(packagename, ex)
        )
        return 1
    info = pypiinfo.get('info', {})
    if not info:
        print_err('No info for package: {}'.format(packagename))
        return 1
    releases = pypiinfo.get('releases', [])
    otherreleasecnt = len(releases) - 1
    releasecntstr = ''
    if otherreleasecnt:
        releasecntstr = C('').join(
            C('+', 'yellow'),
            C(otherreleasecnt, 'blue', style='bright'),
            C(' releases', 'yellow')
        ).join('(', ')', stysle='bright')

    pkgstr = '\n'.join((
        '\n{name:<30} {ver:<10} {releasecnt}',
        '    {summary}',
    )).format(
        name=C(info['name'], 'blue'),
        ver=C(info['version'], 'lightblue'),
        releasecnt=releasecntstr,
        summary=C(
            FormatBlock(info['summary'].strip()).format(
                width=76,
                newlines=True,
                prepend='    ',
                strip_first=True,
            ),
            'cyan'
        ),
    )
    label_color = 'blue'
    value_color = 'cyan'
    subvalue_color = 'lightcyan'
    authorstr = ''
    if info['author'] and info['author'] not in ('UNKNOWN', ):
        authorstr = C(': ').join(
            C('Author', label_color),
            C(info['author'], value_color)
        )
    emailstr = ''
    if info['author_email'] and info['author_email'] not in ('UNKNOWN', ):
        emailstr = C(
            info['author_email'],
            subvalue_color,
        ).join('<', '>', style='bright')

    if authorstr or emailstr:
        pkgstr = '\n'.join((
            pkgstr,
            '    {author}{email}'.format(
                author=authorstr,
                email=' {}'.format(emailstr) if authorstr else emailstr,
            )
        ))

    if info['home_page']:
        homepagestr = C(': ').join(
            C('Homepage', label_color),
            C(info['home_page'], value_color)
        )
        pkgstr = '\n'.join((
            pkgstr,
            '    {homepage}'.format(
                homepage=homepagestr,
            )
        ))
    latestrelease = sorted(releases)[-1] if releases else None
    if latestrelease:
        try:
            latestdls = releases[latestrelease][0].get('downloads', 0)
        except IndexError:
            # Latest release has no info dict.
            latestdls = 0
        alldls = get_pypi_release_dls(releases)

        lateststr = C(' ').join(
            C(': ').join(
                C('Latest', label_color),
                C(latestrelease, value_color)
            ),
            C('').join(
                C(latestdls, 'blue'),
                C(' dls', 'green'),
                ', ',
                C(alldls, 'blue'),
                C(' for all versions', 'green'),
            ).join('(', ')'),
        )

        pkgstr = '\n'.join((
            pkgstr,
            '    {latest}'.format(
                latest=lateststr,
            )
        ))
    print(pkgstr)
    return 0


def show_package_infos(packagenames):
    """ Show local and pypi info for a list of package names.
        Returns 0 on success, otherwise returns the number of errors.
    """
    return sum(show_package_info(name) for name in packagenames)


def sort_requirements(filename=DEFAULT_FILE):
    """ Sort a requirements file, and re-write it. """
    reqs = Requirementz.from_file(filename=filename)
    reqs.write(filename=filename)
    return 0


class FatalError(EnvironmentError):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


@total_ordering
class RequirementPlus(Requirement):
    """ A requirements.requirement.Requirement with extra helper methods.
    """
    def __init__(self, line):
        # Requirement asks that you do not call __init__ directly,
        # but use the parse* class methods (requirement.py:128).
        super().__init__(line)
        # Cache the installed version of this requirement, when needed.
        self._installed_ver = None

    def __eq__(self, other):
        """ RequirementPluses are equal if they have the same specs. """
        if not hasattr(other, 'specs'):
            return False
        return set(self.specs) == set(other.specs)

    def __hash__(self):
        """ hash() implementation for RequirementPlus. """
        return hash(str(self))

    def __lt__(self, other):
        nothing = object()
        othername = getattr(other, 'name', nothing)
        if othername is nothing:
            return False
        if not (self.name < othername):
            return False

        otherspecs = getattr(other, 'specs', nothing)
        try:
            specslt = self.specs < otherspecs
        except TypeError:
            if self.specs:
                # Other has no specs.
                return False
            # Self has no specs.
            return bool(other.specs)
        return specslt

    def __repr__(self):
        return str(self)

    def __str__(self):
        """ String representation of a RequirementPlus, which is compatible
            with a requirements.txt line.
        """
        return self.to_str(color=False)

    @staticmethod
    def compare_versions(ver1, op, ver2):
        """ Compare version strings according to the requirements.txt spec.
            Return True if `ver` satisfies the comparison with `ver2`.
            Arguments:
                ver1  : Installed version string.
                op    : Comparison operator ('>', or '==', or '>=', ..)
                ver2  : Required version string.

            Example:
                compare_versions('1.0.1', '>=', '1.0.0')
                >> True
                compare_versions('2.0.0' '<=', '1.0.0')
                >> False
        """
        opfunc = OP_FUNCS.get(op, OP_FUNCS['>='])
        return opfunc(ver1, ver2)

    def installed_version(self):
        """ Return a RequirementPlus for the installed version of this
            RequirementPlus, or None if it is not installed.
        """
        if self._installed_ver is not None:
            return self._installed_ver

        p = PKGS.get(self.name.lower(), None)
        if p is None:
            self._installed_ver = None
            return None
        try:
            ver = p.parsed_version.base_version
        except AttributeError:
            # Old setuptools, no base_version.
            pcs = []
            for num in p.parsed_version:
                try:
                    # Fails for '*final', 'beta', etc. Ignore it.
                    pcs.append(str(int(num)))
                except ValueError:
                    pass
                ver = '.'.join(pcs)
        self._installed_ver = RequirementPlus.parse(
            ' '.join((p.project_name, '==', ver))
        )
        return self._installed_ver

    def satisfied(self, against=None):
        """ Return True if this requirement is satisfied by the installed
            version. Non-installed packages never satisfy the requirement.
            If no `against` requirement is given, and no installed version
            exists, this always returns False.
            Arguments:
                against  : Requirement/RequirementPlus or version string to
                           test against.
                           Default: installed version Requirement, if any.
        """
        againstreq = self.installed_version() if against is None else against
        if againstreq is None:
            return False
        if isinstance(againstreq, str):
            againstspecs = (('', againstreq), )
        elif hasattr(againstreq, 'specs'):
            againstspecs = againstreq.specs
        else:
            raise TypeError(
                ' '.join((
                    'Expecting version str or Requirement/RequirementPlus,',
                    'got: ({}) {!r}'
                )).format(type(against).__name__, against)
            )
        for _, againstver in againstspecs:
            for op, ver in self.specs:
                if self.compare_versions(againstver, op, ver):
                    return True
        return False

    def spec_string(self, color=False):
        """ Just the spec string ('>= 1.0.0, <= 2.0.0') from this requirement.
        """
        if color:
            return str(
                C(',').join(
                    C(' ').join(
                        C(op),
                        C(ver, 'cyan'),
                    )
                    for op, ver in self.specs
                )
            )
        return ','.join('{} {}'.format(op, ver) for op, ver in self.specs)

    def to_str(self, color=False, align=False):
        """ Like __str__, except colors can be used. """
        if self.local_file:
            if color:
                return str(C(' ').join(
                    C('-e', 'cyan'),
                    C(self.path, 'green'),
                ))
            return ' '.join(('-e', self.path))
        elif self.vcs:
            if color:
                hashstr = str(
                    C('=').join(C('#egg', 'lightblue'), C(self.name, 'green'))
                )
            else:
                hashstr = '#egg={}'.format(self.name)
            if color:
                url = str(
                    C('@').join(
                        C(self.uri, 'blue'),
                        C(self.revision, 'yellow'),
                    )
                )
            else:
                url = '@'.join((self.uri, self.revision))
            spec = ''.join((url, hashstr))
            if color:
                return C(' ').join(
                    C('-e', 'cyan'),
                    spec,
                )
            return ' '.join(('-e', spec))

        # Normal pip package.
        if self.extras:
            if color:
                extras = C(', ').join(
                    C(e, 'cyan')
                    for e in self.extras
                ).join('[', ']', style='bright')
            else:
                extras = '[{}]'.format(', '.join(self.extras))
        else:
            extras = ''
        vers = self.spec_string(color=color)
        name = '{name:<{ljust}}'.format(
            name=C(self.name, 'blue') if color else self.name,
            ljust=25 if align else 0,
        )
        return '{}{} {}'.format(name, extras, vers)


class Requirementz(UserList):
    """ A list of RequirementPlus, with helper methods for reading/writing
        files, sorting, etc.
    """

    def __init__(self, requirements=None):
        super(Requirementz, self).__init__(requirements or tuple())

    def add_line(self, line):
        """ Add a requirement to this list by parsing a line/str.
            Returns True if the requirement was added,
            False if the requirement was replaced.
            Raises ValueError on error.
        """
        try:
            req = RequirementPlus.parse(line)
        except ValueError as ex:
            raise ValueError(
                'Invalid requirement spec.: {}'.format(ex)
            )
        reqname = req.name.lower()
        for i, existingreq in enumerate(self[:]):
            if reqname == existingreq.name.lower():
                debug('Found existing requirement: {}'.format(reqname))
                if req == existingreq:
                    raise ValueError(
                        'Already a requirement: {}'.format(existingreq)
                    )
                debug('...versions are different.')
                # Replace old requirement.
                self[i] = req
                return False
        else:
            # No replacement was found, add the new requirement.
            self.append(req)
        return True

    def check(self, errors_only=False, spec_only=False):
        """ Yield status lines for all requirements in this list. """
        for r in self:
            status = StatusLine(r)
            if errors_only and not status.error:
                continue
            elif spec_only:
                yield status.spec
            else:
                yield str(status)

    def duplicates(self):
        """ Return a dict of {RequirementPlus: number_of_duplicates}
            where number_of_duplicates is requirements.count(requirement) - 1
        """
        names = self.names()
        dupes = {}
        for name in names[:]:
            namecount = names.count(name)
            if namecount == 1:
                continue
            dupes[self.get_byname(name)] = namecount - 1
        return dupes

    @classmethod
    def from_file(cls, filename=DEFAULT_FILE):
        """ Instantiate a Requirementz by reading a requirements.txt and
            parsing it.
        """
        with open(filename, 'r') as f:
            reqs = cls.from_lines(f.readlines())
        # Ensure file is closed before returning the class.
        return reqs

    @classmethod
    def from_lines(cls, lines):
        """ Instantiate a Requirementz from a list of requirements.txt lines.
        """
        return cls(RequirementPlus.parse(l) for l in sorted(lines))

    def get_byname(self, name):
        """ Return the first RequirementPlus found by name.
            Returns None if no requirement could be found.
        """
        for r in self:
            if r.name == name:
                return r
        return None

    def names(self):
        """ Return a tuple of names only from these RequirementPluses. """
        return tuple(r.name for r in self)

    def search(self, pattern, ignorecase=True, reverse=False):
        """ Search RequirementPluses using a text/regex pattern.
            Yield RequirementPluses that match.
            If `reverse` is truthy, yields items that DON'T match.
        """
        strpat = getattr(pattern, 'pattern', pattern)
        flags = re.IGNORECASE if ignorecase else 0
        pat = re.compile(strpat, flags=flags)

        def pat_no_match(r):
            """ RequirementPlus is a match if pattern is NOT found. """
            return pat.search(str(r)) is None

        def pat_match(r):
            """ RequirementPlus is a match if pattern IS found. """
            return pat.search(str(r)) is not None
        is_match = pat_no_match if reverse else pat_match
        for r in self:
            if is_match(r):
                yield r

    def write(self, filename=DEFAULT_FILE):
        """ Write this list of requirements to file. """
        debug('Writing sorted file: {}'.format(filename))
        with SafeWriter(filename, 'w') as f:
            f.write('\n'.join(
                str(r) for r in sorted(self, key=lambda r: r.name)
            ))
            f.write('\n')
        return None


class SafeWriter(object):
    def __init__(self, filename, mode='w'):
        self.filename = filename
        self.mode = mode
        self.f = None
        # This will be set to the backed up file name, if one is made.
        # On success, the backup is removed.
        self.backup = None

    def __enter__(self):
        self.file_backup()
        debug(
            'Opening file for mode \'{s.mode}\': {s.filename}'.format(s=self))
        self.f = open(self.filename, mode=self.mode)
        return self.f

    def __exit__(self, extype, val, tb):
        self.f.close()
        if extype is None:
            # No error occurred, safe to remove backup.
            self.file_backup_remove()
            return False
        if self.backup:
            print_err('A backup file was saved: {}'.format(self.backup))
            return False
        print_err('No backup was saved!')
        return False

    def file_backup(self):
        """ Create a backup copy, in case something fails. """
        if not os.path.exists(self.filename):
            # File doesn't exist, no backup needed.
            return None
        backupfile = '{}.bak'.format(self.filename)
        debug('Creating backup file: {}'.format(backupfile))
        try:
            shutil.copy2(self.filename, backupfile)
        except EnvironmentError as ex:
            raise FatalError(
                format_env_err(
                    filename=self.filename,
                    exc=ex,
                    msg='Failed to backup'
                )
            )
        self.backup = backupfile
        return None

    def file_backup_remove(self):
        """ Remove a backup file, after everything else is done. """
        backupfile = '{}.bak'.format(self.filename)
        if not os.path.exists(backupfile):
            if self.backed_up:
                debug('Backup file does not exist: {}'.format(backupfile))
            return None

        debug('Removing backup file: {}'.format(backupfile))
        try:
            os.remove(backupfile)
        except EnvironmentError as ex:
            raise FatalError(format_env_err(
                filename=backupfile,
                exc=ex,
                msg='Failed to remove backup file'
            ))
        return None


class StatusLine(object):
    def __init__(self, req):
        self.req = req
        self.error = False
        # Cached by self.with_latest() on demand.
        self.pypi_info = None
        self.status_latest = None

        # Init this requirement's status line.
        installedver = req.installed_version()
        includedvers = set(
            ver for op, ver in req.specs if op.endswith('=')
        )
        for op, ver in req.specs:
            if ver == '0':
                requiredver = C('installed', fore='cyan')
                break
        else:
            requiredver = req.spec_string()
        self.spec_name = req.name
        self.spec_ver = requiredver
        if installedver is None:
            # No version installed.
            installverstr = None
            installverfmt = C('not installed', fore='red')
            errstatus = C('!', fore='red')
            self.error = True
        else:
            installverstr = installedver.specs[0][1]
            installverfmt = C(' ').join('v.', C(installverstr, fore='cyan'))
            if req.satisfied():
                # Version installed/required mismatches (still okay)
                if installverstr in includedvers:
                    errstatus = ' '
                else:
                    errstatus = C('-', fore='yellow', style='bright')
            else:
                errstatus = C('!', fore='red', style='bright')
                self.error = True

        verboseerr = C('Error', fore='red', style='bright')
        verboseok = C('Ok', fore='green')
        # Build status line.
        colr_fmt = C(
            '{verbose:<5} {name:<30} {installed:<13} {status} {required:<12}'
        )
        self.status_colr = colr_fmt.format(
            verbose=verboseerr if self.error else verboseok,
            name=C(req.name, fore='blue'),
            installed=installverfmt,
            status=errstatus,
            required=C(
                requiredver,
                fore=('red' if self.error else 'green')
            ),
        )
        self.pkg = PKGS.get(self.req.name, None)
        self.pkg_location = getattr(self.pkg, 'location', None)

    def __str__(self):
        return self.to_str(color=False)

    def location(self, color=False, default=''):
        """ Return the location on disk for this requirement's package,
            if installed. Otherwise return ''.
        """
        s = self.pkg_location or (default or '')
        if color:
            return str(C(s, 'yellow'))
        return s

    def spec(self, color=False):
        """ Return self.spec if color is False, otherwise colorize self.spec
            and return it.
        """
        if not color:
            return '{} {}'.format(self.spec_name, self.spec_ver)
        return str(C(' ').join(
            C(self.spec_name, 'blue'),
            C(self.spec_ver, 'cyan')
        ))

    def status(self, color=False, location=False):
        """ Return a stringified status for this requirement. """
        statusstr = str(
            self.status_colr if color else self.status_colr.stripped()
        )
        if location:
            return ' '.join((
                statusstr,
                self.location(color=color, default='Not Installed')
            ))
        return statusstr

    def with_latest(self, color=False, location=False):
        """ Return this status line, with the latest available version
            appended. This connects to pypi to retrieve the latest.
            Possibly raises urllib.error.HTTPError, UnicodeDecodeError, and
            ValueError from `get_pypi_info()`.
        """
        if self.status_latest:
            # Info is cached for this requirement, no need to contact pypi.
            return self.status_latest

        # Grab pypi info from python.org.
        pypiinfo = get_pypi_info(self.req.name)

        latest = pypiinfo.get('info', {}).get('version', None)
        if latest is None:
            print_err('No version info found for: {name}'.format(
                name=self.req.name
            ))
            return self.status
        self.pypi_info = pypiinfo

        if self.req.satisfied(against=latest):
            reqver = self.req.specs[0][1]
            if reqver == latest:
                markerstr = ' '
                latest_color = 'green'
            else:
                markerstr = C('-', 'yellow', style='bright')
                latest_color = 'yellow'
        else:
            latest_color = 'red'
            markerstr = C('!', 'red', style='bright')

        # 256 color number for lightpurple.
        lightpurple = 63
        latest_colr = C('{} {}').format(
            # Location will be appended after the pypi info.
            self.status(color=color, location=False),
            C(
                '{} {}: {:<10}'.format(
                    markerstr,
                    C('pypi', lightpurple),
                    C(self.pypi_info['info']['version'], fore=latest_color),
                )
            )
        )
        if location:
            latest_colr = C(' ').join((
                latest_colr,
                self.location(color=True),
            ))
        if color:
            self.status_latest = str(latest_colr)
        else:
            self.status_latest = str(latest_colr.stripped())
        return self.status_latest

    def to_str(self, color=False, location=False):
        if location:
            return C(' ').join(
                self.status(color=color),
                self.location(color=color)
            )
        return self.status(color=color)


# Global {package_name: package} dict.
PKGS = load_packages()


if __name__ == '__main__':
    try:
        mainret = main(docopt(USAGESTR, version=VERSIONSTR, script=SCRIPT))
    except (FatalError, HTTPError, UnicodeDecodeError, ValueError) as ex:
        if DEBUG:
            print_err('\n{}\n'.format(traceback.format_exc()))
        else:
            print_err('\n{}\n'.format(ex))
        mainret = 1
    except EnvironmentError as ex:
        if DEBUG:
            print_err(traceback.format_exc())
        else:
            print_err(format_env_err(exc=ex))
        mainret = 1

    sys.exit(mainret)
