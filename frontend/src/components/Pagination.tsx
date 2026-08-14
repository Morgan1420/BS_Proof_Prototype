import React from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';

import { colors, spacing, typography } from '../theme';

export interface PaginationProps {
  /** 0-indexed current page. */
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

/**
 * Shared "← Previous / [1] [2] ... [n] / Next →" pagination footer —
 * factored out of StudiesList.tsx so RecommendedUsesList.tsx (Scientific
 * Information redesign) can render pagination controls with the exact
 * same look and interaction style, per spec, rather than a second,
 * hand-copied implementation that could drift from this one over time.
 *
 * Renders every page number (no "..." truncation for long lists) —
 * matches StudiesList's original behavior exactly; both of this app's
 * current callers have small enough page counts (a handful to a few
 * dozen papers/conclusions per ingredient) that this has never needed
 * truncation.
 *
 * Palette note: hardcoded to `colors.orange` throughout, same reasoning
 * as StudiesList.tsx's own docstring — every current caller only ever
 * renders this while its parent card is already in its all-orange
 * expanded state, so there's no "collapsed" variant to also support.
 */
const Pagination: React.FC<PaginationProps> = ({ page, totalPages, onPageChange }) => {
  if (totalPages <= 1) {
    return null;
  }

  return (
    <View style={styles.paginationRow}>
      <Pressable
        style={[styles.navButton, page === 0 && styles.navButtonDisabled]}
        onPress={() => onPageChange(Math.max(0, page - 1))}
        disabled={page === 0}
        accessibilityRole="button"
        accessibilityLabel="Previous page"
      >
        <Text style={[styles.navButtonText, page === 0 && styles.navButtonTextDisabled]}>
          ← Previous
        </Text>
      </Pressable>

      <View style={styles.pageNumberRow}>
        {Array.from({ length: totalPages }, (_, pageIndex) => (
          <Pressable
            key={pageIndex}
            style={[styles.pageBadge, pageIndex === page && styles.pageBadgeActive]}
            onPress={() => onPageChange(pageIndex)}
            accessibilityRole="button"
            accessibilityLabel={`Go to page ${pageIndex + 1}`}
            accessibilityState={{ selected: pageIndex === page }}
          >
            <Text
              style={[styles.pageBadgeText, pageIndex === page && styles.pageBadgeTextActive]}
            >
              {pageIndex + 1}
            </Text>
          </Pressable>
        ))}
      </View>

      <Pressable
        style={[styles.navButton, page === totalPages - 1 && styles.navButtonDisabled]}
        onPress={() => onPageChange(Math.min(totalPages - 1, page + 1))}
        disabled={page === totalPages - 1}
        accessibilityRole="button"
        accessibilityLabel="Next page"
      >
        <Text
          style={[styles.navButtonText, page === totalPages - 1 && styles.navButtonTextDisabled]}
        >
          Next →
        </Text>
      </Pressable>
    </View>
  );
};

const styles = StyleSheet.create({
  paginationRow: {
    flexDirection: 'row',
    justifyContent: 'center',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: spacing.sm,
    paddingTop: spacing.xs,
  },
  navButton: {
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.xs,
  },
  navButtonDisabled: {
    opacity: 0.4,
  },
  navButtonText: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  navButtonTextDisabled: {
    color: `${colors.orange}88`,
  },
  pageNumberRow: {
    flexDirection: 'row',
    gap: spacing.xs,
  },
  pageBadge: {
    borderWidth: 1,
    borderColor: colors.orange,
    borderRadius: 4,
    paddingHorizontal: 8,
    paddingVertical: 2,
    minWidth: 26,
    alignItems: 'center',
  },
  pageBadgeActive: {
    backgroundColor: colors.orange,
    borderColor: colors.orange,
  },
  pageBadgeText: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.orange,
  },
  pageBadgeTextActive: {
    color: colors.offWhite,
  },
});

export default Pagination;
