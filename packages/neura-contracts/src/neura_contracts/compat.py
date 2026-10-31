"""What Python 3.11 added that neura-robot uses, for robot computers still on 3.10 (JetPack 6,
ROS 2 Humble): ``StrEnum``, whose members are their string values."""

from __future__ import annotations

import sys
from enum import Enum

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:

    class StrEnum(str, Enum):
        """``enum.StrEnum`` as 3.11 defines it: ``str(member)`` is its value."""

        def __str__(self) -> str:
            return str(self.value)


__all__ = ["StrEnum"]
