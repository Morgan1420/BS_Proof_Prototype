import React from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';

import { colors } from '../theme';
import { GRADE_COLORS } from '../utils/grades';
import type { PaperGrade } from '../services/api';

/** Small (row) / large (modal header) circular diameters — exported so
 * callers that render their own non-lettered badge variant next to this
 * one (StudiesList's gray "(-)"/loading ungraded badge) can match its
 * footprint exactly without duplicating the magic numbers. */
export const GRADE_CIRCLE_SIZE = 26;
export const GRADE_CIRCLE_SIZE_LARGE = 44;

export interface GradeCircleBadgeProps {
  grade: PaperGrade;
  /** Omit entirely to render a plain, non-pressable badge — used for
   * every "Rubric & Comments" modal header, where the badge is already
   * what got tapped to open that modal. */
  onPress?: () => void;
  /** Larger variant used in modal headers — the per-row badge stays
   * small so it doesn't crowd the row's title text. */
  large?: boolean;
  accessibilityLabel?: string;
}

/**
 * Shared round letter-grade badge (A-E, `GRADE_COLORS` fill, white bold
 * letter, palette-orange border) — factored out of StudiesList.tsx's own
 * `PaperGradeBadge` so RecommendedUsesList.tsx and
 * VerifiedResourcesList.tsx render an identical badge for their own
 * graded entities (`PaperConclusion.confidence_grade`,
 * `VerifiedResource.grade`) instead of three hand-copied, driftable
 * implementations. Tapping it (when `onPress` is supplied) is every
 * list's trigger for its "Rubric & Comments Modal" — see each list's
 * `activeRubricModalItem` state.
 */
const GradeCircleBadge: React.FC<GradeCircleBadgeProps> = ({
  grade,
  onPress,
  large = false,
  accessibilityLabel,
}) => {
  const badgeStyle = [
    styles.badge,
    large ? styles.badgeLarge : null,
    { backgroundColor: GRADE_COLORS[grade] },
  ];
  const textStyle = [styles.text, large && styles.textLarge];

  if (!onPress) {
    return (
      <View style={badgeStyle}>
        <Text style={textStyle}>{grade}</Text>
      </View>
    );
  }

  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel ?? `View rubric breakdown — grade ${grade}`}
      hitSlop={6}
      style={badgeStyle}
    >
      <Text style={textStyle}>{grade}</Text>
    </Pressable>
  );
};

const styles = StyleSheet.create({
  badge: {
    width: GRADE_CIRCLE_SIZE,
    height: GRADE_CIRCLE_SIZE,
    borderRadius: GRADE_CIRCLE_SIZE / 2,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1.5,
    borderColor: colors.orange,
  },
  badgeLarge: {
    width: GRADE_CIRCLE_SIZE_LARGE,
    height: GRADE_CIRCLE_SIZE_LARGE,
    borderRadius: GRADE_CIRCLE_SIZE_LARGE / 2,
    borderWidth: 2,
  },
  text: {
    fontSize: 12,
    fontWeight: '700',
    color: '#FFFFFF',
  },
  textLarge: {
    fontSize: 18,
  },
});

export default GradeCircleBadge;
