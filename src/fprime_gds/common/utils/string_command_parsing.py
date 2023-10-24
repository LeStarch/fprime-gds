import json
import re
from typing import List

# Regular-expression used to detect quoted strings (allowing internal escaped quotes) and unquoted names (abc.def)
STRING_PREPROCESSOR_EXPRESSION = re.compile(f'(?:"((?:[^"]|\\")*)")|([a-zA-Z_][a-zA-Z_0-9.]*)')


class CommandParsingException(Exception):
    """ Exception caused during command parsing """
    def __init__(self):
        """ Initialize the exception with an error message """
        super().__init__("[TIME], FULL_COMMAND_NAME[[[, ARG1], ARG2], ...]")


def parse_command_from_string(command_string: str) -> List:
    """ Parse a fprime sequence arguments from a substring into tokens

    Given the string representing the fprime command, return a list of argument tokens parsed out of that string in
    python format (strings, numbers, etc). This supports the pseudo-JSON format of complex arguments.

    A command string is an optional time format, command name, and command delimited list of arguments where an argument
    may be:
    - A JSON primitive (number, boolean, quoted string)
    - An unquoted string of characters representing an enumeration value
    - A JSON composite type containing sub arguments (list, dictionary)

    Args:
        string: substring to parse into arguments
    Returns:
        list of arguments as native python data-types (numbers, booleans, strings, dictionaries, and lists)
    """
    try:
        # The goal here is to convert the almost-JSON command argument string into a proper JSON string that represents
        # a list of arguments. This is done in two steps:
        #   1. Covert the unquoted strings to quoted strings
        #   2. Place the whole thing in [ ]'s
        # Then it may just be loaded using json.loads
        properly_quoted_strings = STRING_PREPROCESSOR_EXPRESSION.sub('"\1\2\3"', command_string)
        json_list_string = f"[{properly_quoted_strings}]"
        return json.loads(json_list_string)
    except Exception as exception:
        raise CommandParsingException()
