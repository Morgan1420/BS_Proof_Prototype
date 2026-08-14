import React, { useMemo } from 'react';
import { View, Text, StyleSheet } from 'react-native';

import { colors, spacing, typography } from '../theme';
import type { ResearchPaper } from '../services/api';
import { isPaperGrade } from '../utils/grades';

/** Mirrors docs/paper_grading_rubric.json's `grade_bands` (A: 85-100
 * down to E: 0-29) — duplicated here purely to render an *approximate*
 * average-grade letter next to the average numeric score. The frontend
 * has no access to that backend JSON file at build/runtime, and this is
 * a display-only convenience, not a scoring decision — the backend
 * remains the single source of truth for every individual paper's own
 * `grade`/`grade_score`, which this only averages. */
const GRADE_BANDS: ReadonlyArray<{ grade: string; min: number }> = [
  { grade: 'A', min: 85 },
  { grade: 'B', min: 70 },
  { grade: 'C', min: 50 },
  { grade: 'D', min: 30 },
  { grade: 'E', min: 0 },
];

function scoreToGrade(score: number): string {
  return GRADE_BANDS.find((band) => score >= band.min)?.grade ?? 'E';
}

export interface StudiesAnalysisBarProps {
  /** Every stored ResearchPaper for this ingredient (same array
   * StudiesList renders) — `undefined` while still loading. */
  papers: ResearchPaper[] | undefined;
}

/**
 * "Studies Analisis" summary bar — sits directly above StudiesList in
 * the redesigned Scientific Information section (see IngredientCard.tsx).
 * Purely derived from the same `papers` array StudiesList already
 * receives; owns no fetching/state of its own.
 */
const StudiesAnalysisBar: React.FC<StudiesAnalysisBarProps> = ({ papers }) => {
  const { totalStudies, averageGradeLabel } = useMemo(() => {
    const list = papers ?? [];
    const gradedScores = list
      .filter(
        (paper): paper is ResearchPaper & { grade_score: number } =>
          isPaperGrade(paper.grade) && typeof paper.grade_score === 'number'
      )
      .map((paper) => paper.grade_score);

    if (gradedScores.length === 0) {
      return { totalStudies: list.length, averageGradeLabel: 'N/A' };
    }

    const average =
      gradedScores.reduce((sum, score) => sum + score, 0) / gradedScores.length;
    const rounded = Math.round(average);
    return {
      totalStudies: list.length,
      averageGradeLabel: `${scoreToGrade(rounded)} (${rounded})`,
    };
  }, [papers]);

  return (
    <View style={styles.container}>
      <Text style={styles.header}>Studies Analisis</Text>
      <View style={styles.metricsRow}>
        <Text style={styles.metric}>Total studies: {totalStudies}</Text>
        <Text style={styles.metric}>Average grade: {averageGradeLabel}</Text>
        {/* Placeholder per spec — not yet computed from real data. */}
        <Text style={styles.metric}>Rating: XX</Text>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    gap: spacing.xs,
  },
  header: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
    textAlign: 'center',
    letterSpacing: 0.5,
  },
  metricsRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
    gap: spacing.sm,
  },
  metric: {
    fontSize: typography.resultCardLabel,
    fontWeight: '600',
    color: colors.orange,
  },
});

export default StudiesAnalysisBar;
