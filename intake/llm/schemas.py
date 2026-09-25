"""LLM tool definitions and the pydantic schema for submit_decision (owner C1/C2).

The LLM tool list is EXACTLY these five, all read-only. There is no write tool:
the runner writes status itself, and code decides (the LLM can only downgrade).
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

LLM_TOOL_NAMES = ('detect_tc', 'find_schedules', 'get_schedule', 'check_brand_overlap', 'submit_decision')

REASON_CODES = (
    'columns_unrecognised', 'pdf_disagreement', 'shared_tc', 'no_schedule', 'multiple_schedules',
    'conflict', 'schedule_frozen', 'schedule_locked', 'duplicate_active_number', 'date_out_of_range',
    'foreign_brands', 'low_brand_overlap', 'no_tc_attachment', 'suspicious_instruction', 'tool_error',
)


class Decision(BaseModel):
    """submit_decision input. No confidence field (owner C2)."""
    model_config = ConfigDict(extra='forbid')
    attachment_id: int
    decision: Literal['propose', 'needs_review', 'ignore']
    schedule_id: Optional[int] = None
    reason_code: Optional[Literal[REASON_CODES]] = None
    schedule_number_source: Literal['subject', 'body', 'file_name', 'file_content', 'none']
    suspicious_instruction: bool
    note: str = Field(max_length=200)


def _obj(props: dict, required: list) -> dict:
    return {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}


_ATT = {'attachment_id': {'type': 'integer'}}
_SCH = {'schedule_id': {'type': 'integer'}}

TOOLS = [
    {'name': 'detect_tc', 'strict': True,
     'description': 'Read the attachment: file type, channel guess, TC date range, row count, up to 20 '
                    'sample themes, missing columns, skipped rows; for PDFs whether the two readers '
                    'disagree and whether AI reading was available.',
     'input_schema': _obj(_ATT, ['attachment_id'])},
    {'name': 'find_schedules', 'strict': True,
     'description': 'Active candidate schedules whose channel and period fit the file.',
     'input_schema': _obj(_ATT, ['attachment_id'])},
    {'name': 'get_schedule', 'strict': True,
     'description': 'Facts about one candidate schedule returned by find_schedules.',
     'input_schema': _obj(_SCH, ['schedule_id'])},
    {'name': 'check_brand_overlap', 'strict': True,
     'description': 'How many TC rows resolve to the candidate schedule\'s brands: total, candidate, '
                    'foreign, overlap, passes.',
     'input_schema': _obj({**_ATT, **_SCH}, ['attachment_id', 'schedule_id'])},
    {'name': 'submit_decision', 'strict': True,
     'description': 'Your final answer. Call it exactly once, at the end.',
     'input_schema': _obj({
         'attachment_id': {'type': 'integer'},
         'decision': {'type': 'string', 'enum': ['propose', 'needs_review', 'ignore']},
         'schedule_id': {'type': ['integer', 'null']},
         'reason_code': {'type': ['string', 'null'], 'enum': [*REASON_CODES, None]},
         'schedule_number_source': {'type': 'string',
                                    'enum': ['subject', 'body', 'file_name', 'file_content', 'none']},
         'suspicious_instruction': {'type': 'boolean'},
         'note': {'type': 'string'},
     }, ['attachment_id', 'decision', 'schedule_id', 'reason_code', 'schedule_number_source',
         'suspicious_instruction', 'note'])},
]
assert tuple(t['name'] for t in TOOLS) == LLM_TOOL_NAMES
