"""
@brief Class to store a specific channel telemetry reading

@date Created July 2, 2018
@author R. Joseph Paetz

@bug No known bugs
"""

import datetime

from fprime_gds.common.models.serialize import time_type
from fprime_gds.common.models.serialize.array_type import ArrayType
from fprime_gds.common.models.serialize.serializable_type import SerializableType

from fprime_gds.common.data_types import sys_data
from fprime_gds.common.utils.string_util import format_string_template


# Sentinel indicating display_text has not yet been computed.
_DISPLAY_TEXT_UNSET = object()


class ChData(sys_data.SysData):
    """
    The ChData class stores a specific channel telemetry reading.
    """

    def __init__(self, ch_val_obj, ch_time, ch_temp):
        """
        Constructor.

        Args:
            ch_val_obj: The channel's value at the given time. Should be an
                        instance of a class derived from the BaseType class
                        (with a deserialized data value) or None
            ch_temp: Channel template instance for this channel
            ch_time: Time the reading was made

        Returns:
            An initialized ChData object
        """
        super().__init__()
        self.id = ch_temp.get_id()
        self.val_obj = ch_val_obj
        self.time = ch_time
        self.template = ch_temp
        self.pkt = None
        self._display_text = _DISPLAY_TEXT_UNSET
        # Earth Received Time - timestamp when GDS created this channel data object
        self.ert = datetime.datetime.now(datetime.timezone.utc)

    @staticmethod
    def get_empty_obj(ch_temp):
        """
        Obtains a channel object that is empty (has a value of None)

        Args:
            ch_temp: (ChTemplate object) Template describing the channel

        Returns:
            A ChData Object with ch value of None
        """
        return ChData(None, time_type.TimeType(), ch_temp)

    @property
    def display_text(self):
        """Lazy-evaluated display text.

        Returns *None* when the channel template has no format string
        (the UI falls back to the raw ``val`` in that case).  When a
        format string IS configured, the formatted representation is
        computed on first access and cached.
        """
        if self._display_text is _DISPLAY_TEXT_UNSET:
            self._display_text = self._compute_display_text()
        return self._display_text

    def _compute_display_text(self):
        """Compute the formatted display text for this channel reading.

        Returns *None* when the channel template has no format string,
        signalling that downstream consumers should use the raw value.
        """
        val_obj = self.val_obj
        template = self.template
        # Empty objects (e.g. when listing channels) show the description
        if val_obj is None:
            return template.ch_desc
        fmt_str = template.get_format_str()
        # No format string → skip expensive formatting; UI uses val directly
        if not fmt_str:
            return None
        temp_val = (
            val_obj.val
            if not isinstance(val_obj, (SerializableType, ArrayType))
            else val_obj.formatted_val
        )
        if temp_val is None:
            return ""
        return format_string_template(fmt_str, (temp_val,))

    def set_pkt(self, pkt):
        """
        Set the packet object to which this channel belongs (can be None)

        Args:
            pkt: The packet object to which these channels were transmitted in
        """
        self.pkt = pkt

    def get_pkt(self):
        """
        Return the packet object to which this channel belongs (could be None)

        Returns:
            The channel's packet
        """
        return self.pkt

    def get_val(self):
        """
        Return the channel value

        Returns:
            The channel reading
        """
        return None if self.val_obj is None else self.val_obj.val

    def get_val_obj(self):
        """
        Return the channel's value object

        Returns:
            The channel's value object containing the value (obj of a type
            inherited from TypeBase
        """
        return self.val_obj

    def get_display_text(self):
        """
        Convert the channel value to a string, using the format specifier if provided.
        Falls back to the raw value when no format string is configured.
        """
        text = self.display_text
        if text is None:
            if self.val_obj is None:
                return ""
            val = self.val_obj.val
            return val if val is not None else ""
        return text

    @staticmethod
    def get_csv_header(verbose=False):
        """
        Get the header for a csv file containing channel data

        Args:
            verbose: (boolean, default=False) Indicates if header should be for
                                              regular or verbose output

        Returns:
            Header for a csv file containing channel data
        """
        return "Time,Raw Time,Name,ID,Value\n" if verbose else "Time,Name,Value\n"

    def get_dict(self, time_zone=None):
        """
        Convert the channel data to a dictionary

        Args:
            time_zone: (tzinfo, default=None) Timezone to print time in. If
                      time_zone=None, use local time.

        Returns:
            Dictionary version of the channel data
        """
        return {
            "time": self.time.to_readable(time_zone),
            "raw_time": str(self.time),
            "name": self.template.get_full_name(),
            "id": self.id,
            "display_text": self.get_display_text(),
        }

    def get_str(self, time_zone=None, verbose=False, csv=False):
        """
        Convert the channel data to a string

        Args:
            time_zone: (tzinfo, default=None) Timezone to print time in. If
                      time_zone=None, use local time.
            verbose: (boolean, default=False) Prints extra fields if True
            csv: (boolean, default=False) Prints each field with commas between
                                          if true

        Returns:
            String version of the channel data
        """
        time_str_nice = self.time.to_readable(time_zone)
        raw_time_str = str(self.time)
        ch_name = self.template.get_full_name()
        display_text = self.get_display_text()

        if verbose and csv:
            return f"{time_str_nice},{raw_time_str},{ch_name},{self.id},{display_text}"
        if verbose and not csv:
            return (
                f"{time_str_nice}: {ch_name} ({self.id}) {raw_time_str} {display_text}"
            )
        if not verbose and csv:
            return f"{time_str_nice},{ch_name},{display_text}"
        return f"{time_str_nice}: {ch_name} = {display_text}"

    def __str__(self):
        """
        Convert the ch data to a string

        Returns:
            String version of the channel data
        """
        return self.get_str()
