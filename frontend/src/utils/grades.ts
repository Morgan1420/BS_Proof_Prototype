import type { PaperGrade } from '../services/api';

/**
 * Shared letter-grade helpers — factored out of StudiesList.tsx so the
 * new RecommendedUsesList.tsx (Scientific Information redesign) can
 * apply the exact same grade->color mapping and "C or higher"/sort-rank
 * logic to conclusion grades as StudiesList already applies to paper
 * grades, without copy-pasting a second, driftable copy.
 *
 * `PaperConclusion.confidence_grade` and `VerifiedResource.grade` (see
 * services/api.ts) both use the same A-E letter scale as
 * `ResearchPaper.grade` — all three are ultimately server-derived from a
 * 0-100 rubric total via that rubric's `grade_bands`
 * (docs/paper_grading_rubric.json / docs/conclusion_grading_rubric.json /
 * docs/resource_grading_rubric.json), so sharing one `PaperGrade` type
 * and one color/rank mapping between all of them is intentional, not a
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

/** Mirrors docs/paper_grading_rubric.json's `grade_bands` (A: 85-100 down
 * to E: 0-29) — moved here from the now-deleted StudiesAnalysisBar.tsx
 * (Scientific Information redesign) so IngredientCard.tsx's synthesized
 * summary sentence can compute the same *approximate* average-grade
 * letter that component used to show, without duplicating the band table
 * a second time. Display-only, same caveat as before: the frontend has
 * no access to the backend's rubric JSON at build/runtime, and this
 * never decides any individual paper's own `grade`/`grade_score` — it
 * only averages already-server-assigned scores. */
const GRADE_BANDS: ReadonlyArray<{ grade: PaperGrade; min: number }> = [
  { grade: 'A', min: 85 },
  { grade: 'B', min: 70 },
  { grade: 'C', min: 50 },
  { grade: 'D', min: 30 },
  { grade: 'E', min: 0 },
];

function scoreToGrade(score: number): PaperGrade {
  return GRADE_BANDS.find((band) => score >= band.min)?.grade ?? 'E';
}

export interface AverageGradeResult {
  /** How many of the input items actually had both a recognized grade
   * and a numeric score — i.e. how many contributed to the average. */
  gradedCount: number;
  /** Rounded 0-100 average of every graded item's score, or `null` if
   * `gradedCount` is 0 (nothing to average yet). */
  averageScore: number | null;
  /** Letter grade for `averageScore` via the band table above, or `null`
   * alongside a null `averageScore`. */
  averageGrade: PaperGrade | null;
}

/** Averages `grade_score`-shaped fields across any list of graded items
 * (ResearchPaper, PaperConclusion, or VerifiedResource all fit — see the
 * generic constraint) — used by IngredientCard.tsx to build the
 * Scientific Information section's synthesized summary sentence. Items
 * missing a recognized grade or a numeric score are simply excluded from
 * the average rather than treated as 0, same convention
 * StudiesAnalysisBar.tsx used. */
export function computeAverageGrade(
  items: ReadonlyArray<{ grade?: string | null; score?: number | null }>
): AverageGradeResult {
  const gradedScores = items
    .filter(
      (item): item is { grade: string; score: number } =>
        isPaperGrade(item.grade) && typeof item.score === 'number'
    )
    .map((item) => item.score);

  if (gradedScores.length === 0) {
    return { gradedCount: 0, averageScore: null, averageGrade: null };
  }

  const average = gradedScores.reduce((sum, score) => sum + score, 0) / gradedScores.length;
  const rounded = Math.round(average);
  return {
    gradedCount: gradedScores.length,
    averageScore: rounded,
    averageGrade: scoreToGrade(rounded),
  };
}
