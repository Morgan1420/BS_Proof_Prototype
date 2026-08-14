import type { PaperGrade } from '../services/api';

/**
 * Shared letter-grade helpers — factored out of StudiesList.tsx so the
 * new RecommendedUsesList.tsx (Scientific Information redesign) can
 * apply the exact same grade->color mapping and "C or higher"/sort-rank
 * logic to conclusion grades as StudiesList already applies to paper
 * grades, without copy-pasting a second, driftable copy.
 *
 * `PaperConclusion.confidence_grade` (see services/api.ts) uses the same
 * A-E letter scale as `ResearchPaper.grade` — both are ultimately
 * server-derived from a 0-100 rubric total via that rubric's
 * `grade_bands` (docs/paper_grading_rubric.json /
 * docs/conclusion_grading_rubric.json), so sharing one `PaperGrade` type
 * and one color/rank mapping between them is intentional, not a
 * coincidence.
 */

/** Fixed grade->color mapping, per spec — deliberately NOT sourced from
 * theme.ts's palette: these are semantic quality-signal colors (traffic-
 * light-style green-to-red), not part of the app's brand palette. */
export const GRADE_COLORS: Record<PaperGrade, string> = {
  A: '#1E7E34',
  B: '#28A745',
  C: '#D39E00',
  D: '#FD7E14',
  E: '#DC3545',
};

/** Narrows a loosely-typed `grade?: string | null` (as returned by the
 * API for both ResearchPaper.grade and PaperConclusion.confidence_grade)
 * down to a known PaperGrade. */
export function isPaperGrade(value: string | null | undefined): value is PaperGrade {
  return value === 'A' || value === 'B' || value === 'C' || value === 'D' || value === 'E';
}

/** Grade -> sort/threshold rank, lowest number = best grade. Used both
 * for StudiesList's grade-based sort and RecommendedUsesList's "C or
 * higher" filter (rank <= GRADE_RANK.C). */
export const GRADE_RANK: Record<PaperGrade, number> = { A: 1, B: 2, C: 3, D: 4, E: 5 };
/** Rank assigned to an ungraded/unrecognized value — below every real
 * letter grade, so it always sorts last / fails any "grade X or better"
 * threshold check. */
export const UNGRADED_RANK = 6;

export function getGradeRank(grade: string | null | undefined): number {
  return isPaperGrade(grade) ? GRADE_RANK[grade] : UNGRADED_RANK;
}
