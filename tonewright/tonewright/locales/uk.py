"""Ukrainian (українська) interface catalogue. Format: see tonewright/i18n.py."""
from . import uk_part1, uk_part2, uk_part3, uk_part4, uk_part5, uk_part6

MESSAGES: dict = {}
for _part in (uk_part1, uk_part2, uk_part3, uk_part4, uk_part5, uk_part6):
    MESSAGES.update(_part.MESSAGES)
