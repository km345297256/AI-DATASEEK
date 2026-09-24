"""Internal transient model-request types; never persisted as session messages."""

from langchain_core.messages import HumanMessage
from pydantic import Field


class ToolImageObservationMessage(HumanMessage):
    """Provider-only tool pixels, inseparable from their completed tool batch.

    An actual user message cannot opt into this role through content or public
    message metadata. The internal batch identity is excluded from serialization
    and is consumed only by request-context compaction.
    """

    tool_call_ids: tuple[str, ...] = Field(exclude=True, repr=False)
