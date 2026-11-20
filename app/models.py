from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkRef(BaseModel):
    """A research output and the DOIs it currently owns."""

    id: int
    dois: list[str]


class ImportWorkIn(BaseModel):
    work_id: int = Field(gt=0)
    dois: list[str] = Field(min_length=1)


class ImportCitationIn(BaseModel):
    citing: str = Field(min_length=1)
    cited: str = Field(min_length=1)


class ImportRequest(BaseModel):
    works: list[ImportWorkIn] = Field(default_factory=list)
    citations: list[ImportCitationIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _non_empty(self) -> "ImportRequest":
        if not self.works and not self.citations:
            raise ValueError("import must contain works or citations")
        return self


class ImportResult(BaseModel):
    version: int
    works: int
    dois: int
    citations: int
    internal_citations_ignored: int
    duplicate_citations_ignored: int
    index_edges: int


class RevisionIn(BaseModel):
    """A librarian submission: the version being looked at plus DOI -> work
    assignments that correct the grouping."""

    version: int = Field(ge=1)
    assignments: dict[str, int] = Field(min_length=1)

    @field_validator("assignments")
    @classmethod
    def _positive_work_ids_and_dois(cls, v: dict[str, int]) -> dict[str, int]:
        for raw_doi, work_id in v.items():
            if not raw_doi.strip():
                raise ValueError("DOI keys must not be blank")
            if work_id <= 0:
                raise ValueError(f"work id for DOI {raw_doi!r} must be positive")
        return v


class RevisionResult(BaseModel):
    version: int
    dois_reassigned: int
    works_affected: int
    citations_added: int
    citations_removed: int
    reindexed: bool


class CitationEdge(BaseModel):
    work: WorkRef
    doi_count: int


class CitationResponse(BaseModel):
    version: int
    direction: str
    work: WorkRef
    total_works: int
    total_doi_citations: int
    results: list[CitationEdge]


class WorkDetail(BaseModel):
    id: int
    dois: list[str]
