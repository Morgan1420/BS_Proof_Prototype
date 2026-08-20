/**
 * Shared resource-conclusion alignment helpers (Phase 22) — color/label
 * lookups for the three classification values
 * `VerifiedResource.aligned_conclusions[].alignment` can hold (see
 * backend/app/services/resource_aligner.py and that column's docstring
 * in backend/app/models/research.py). Used by VerifiedResourcesList.tsx's
 * resource info modal to render a colored status badge per extracted
 * conclusion.
 *
 * Deliberately NOT sourced from theme.ts's strict brand palette — same
 * reasoning as utils/grades.ts's `GRADE_COLORS`: these are semantic,
 * traffic-light-style status-signal colors (green "agrees" / red
 * "contradicts" / blue "new information"), not part of the app's brand
 * palette theme.ts otherwise enforces everywhere else in the UI.
 */

/** The three values `resource_aligner.py`'s Gemini schema is constrained
 * to (`Literal["AGREES", "CONTRADICTS", "DISTINCT_NEW"]`) — mirrored here
 * as a string-literal union for the frontend's own narrowing, even though
 * `AlignedConclusion.alignment` itself is typed loosely as `string` at
 * the API boundary (see services/api.ts's doc-comment for why: the
 * backend column isn't enforced beyond its Pydantic Literal by a DB
 * constraint, same convention as VerifiedResource.grade/PaperGrade). */
export type AlignmentValue = 'AGREES' | 'CONTRADICTS' | 'DISTINCT_NEW';

/** Fixed alignment->color mapping, per the Phase 22 task spec (🟢 Agrees /
 * 🔴 Contradicts / 🔵 Distinct-New). */
export const ALIGNMENT_COLORS: Record<AlignmentValue, string> = {
  AGREES: '#28A745',
  CONTRADICTS: '#DC3545',
  DISTINCT_NEW: '#0D6EFD',
};

/** Human-readable badge text per alignment value. */
export const ALIGNMENT_LABELS: Record<AlignmentValue, string> = {
  AGREES: 'Agrees',
  CONTRADICTS: 'Contradicts',
  DISTINCT_NEW: 'Distinct / New',
};

/** Fallback gray for any value that isn't one of the three recognized
 * ones — should only ever be hit defensively (e.g. a future backend
 * label this frontend build predates), same "never crash on an
 * unrecognized server value" posture as `utils/grades.ts`'s
 * `UNGRADED_RANK` handling. */
const NEUTRAL_COLOR = '#6C757D';

export function isAlignmentValue(value: string | null | undefined): value is AlignmentValue {
  return value === 'AGREES' || value === 'CONTRADICTS' || value === 'DISTINCT_NEW';
}

export function getAlignmentColor(value: string | null | undefined): string {
  return isAlignmentValue(value) ? ALIGNMENT_COLORS[value] : NEUTRAL_COLOR;
}

export function getAlignmentLabel(value: string | null | undefined): string {
  return isAlignmentValue(value) ? ALIGNMENT_LABELS[value] : 'Unclassified';
}
