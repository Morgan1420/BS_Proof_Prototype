import React from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import { colors, spacing, typography } from '../theme';

/**
 * A single ingredient/nutrient row. This card is used in two different
 * contexts with two different data shapes available:
 *
 * 1. Nested inside a ProductCard: `amount`/`unit`/`dailyValue` are that
 *    product's specific dosage for this ingredient (from
 *    ProductIngredientLink on the backend).
 * 2. Standalone on ResultsScreen (a bare ingredient search result):
 *    there's no single product's dosage to show, since Ingredient is now
 *    canonical/shared data — instead `recommendedDailyDosage`/
 *    `scientificData`/`productCount` (from the canonical Ingredient row)
 *    are populated instead. See ResultsScreen.tsx::toIngredient().
 *
 * All of these are optional and the component renders whichever set is
 * actually present.
 */
export interface Ingredient {
  id: number;
  productId?: number;
  name: string;
  // Product-specific dosage (context 1 above).
  amount?: string;
  unit?: string;
  dailyValue?: string;
  productName?: string;
  // Canonical ingredient metadata (context 2 above).
  recommendedDailyDosage?: string;
  scientificData?: string;
  productCount?: number;
}

export interface IngredientCardProps {
  ingredient: Ingredient;
  /** Whether this card is currently expanded. Controlled by the parent
   * (ProductCard for nested ingredients, or a screen for standalone
   * results) so only one card in a group can be open at once. */
  isExpanded: boolean;
  /** Called when the header is tapped; the parent decides what "open"
   * means (usually: toggle this id, closing any other open sibling). */
  onToggle: () => void;
}

/** Accordion card for a single ingredient. Expansion state is entirely
 * controlled by the parent — see IngredientCardProps.isExpanded/onToggle
 * — so a group of these can implement single-expansion (only one open at
 * a time) by sharing one `expandedId` state value.
 */
const IngredientCard: React.FC<IngredientCardProps> = ({
  ingredient,
  isExpanded,
  onToggle,
}) => {
  const doseSummary =
    ingredient.amount && ingredient.unit
      ? `${ingredient.amount}${ingredient.unit}`
      : ingredient.recommendedDailyDosage
      ? `RDA: ${ingredient.recommendedDailyDosage}`
      : 'dosage unavailable';

  return (
    <View style={styles.card}>
      <Pressable
        style={styles.headerRow}
        onPress={onToggle}
        accessibilityRole="button"
        accessibilityLabel={`${isExpanded ? 'Collapse' : 'Expand'} ${ingredient.name}`}
        accessibilityState={{ expanded: isExpanded }}
      >
        <Text style={styles.headerText} numberOfLines={2}>
          {ingredient.name} — {doseSummary}
        </Text>
        <Ionicons
          name={isExpanded ? 'chevron-up' : 'chevron-down'}
          size={18}
          color={colors.brown}
        />
      </Pressable>

      {isExpanded && (
        <View style={styles.expandedSection}>
          <View style={styles.doseBlock}>
            {ingredient.amount && ingredient.unit ? (
              // Product-specific dosage — this card represents a
              // particular product's link to this ingredient.
              <View style={styles.doseRow}>
                <Text style={styles.doseLabel}>Dosage</Text>
                <Text style={styles.doseValue}>
                  {`${ingredient.amount} ${ingredient.unit}`}
                </Text>
              </View>
            ) : (
              // No product-specific dosage available (standalone
              // ingredient result) — fall back to the canonical
              // ingredient's general dosage guidance placeholder.
              <View style={styles.doseRow}>
                <Text style={styles.doseLabel}>Recommended Daily Dosage</Text>
                <Text style={styles.doseValue}>
                  {ingredient.recommendedDailyDosage ?? 'Not available'}
                </Text>
              </View>
            )}
            {ingredient.dailyValue && (
              <View style={styles.doseRow}>
                <Text style={styles.doseLabel}>% Daily Value</Text>
                <Text style={styles.doseValue}>{ingredient.dailyValue}</Text>
              </View>
            )}
            {ingredient.productName && (
              <View style={styles.doseRow}>
                <Text style={styles.doseLabel}>Product</Text>
                <Text style={styles.doseValue}>{ingredient.productName}</Text>
              </View>
            )}
            {typeof ingredient.productCount === 'number' && (
              <View style={styles.doseRow}>
                <Text style={styles.doseLabel}>Found In</Text>
                <Text style={styles.doseValue}>
                  {ingredient.productCount}{' '}
                  {ingredient.productCount === 1 ? 'product' : 'products'}
                </Text>
              </View>
            )}
          </View>

          <View style={styles.researchPlaceholder}>
            <Text style={styles.researchPlaceholderText}>
              {ingredient.scientificData ??
                'General compound research & metadata coming soon...'}
            </Text>
          </View>
        </View>
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.offWhite,
    borderWidth: 1,
    borderColor: `${colors.olive}55`,
    borderRadius: 10,
    overflow: 'hidden',
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
  },
  headerText: {
    flex: 1,
    fontSize: typography.body,
    fontWeight: '600',
    color: colors.brown,
  },
  expandedSection: {
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.md,
    gap: spacing.sm,
  },
  doseBlock: {
    backgroundColor: `${colors.olive}18`,
    borderRadius: 8,
    padding: spacing.sm,
    gap: 4,
  },
  doseRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  doseLabel: {
    fontSize: 12,
    fontWeight: '700',
    color: colors.brown,
  },
  doseValue: {
    fontSize: 12,
    color: colors.brown,
    flexShrink: 1,
    textAlign: 'right',
  },
  researchPlaceholder: {
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: `${colors.brown}55`,
    borderRadius: 8,
    padding: spacing.sm,
  },
  researchPlaceholderText: {
    fontSize: 12,
    fontStyle: 'italic',
    color: `${colors.brown}AA`,
    textAlign: 'center',
  },
});

export default IngredientCard;
