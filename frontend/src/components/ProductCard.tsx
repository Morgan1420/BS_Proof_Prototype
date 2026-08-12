import React, { useCallback, useState } from 'react';
import { View, Text, Pressable, StyleSheet } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import IngredientCard from './IngredientCard';
import type { Ingredient } from './IngredientCard';
import { colors, spacing, typography } from '../theme';

/**
 * A single scanned product, with its ingredients. `ingredients` is
 * populated from GET /api/v1/supplements/search's nested per-product
 * data (see ResultsScreen.tsx::toProduct) — `servingSize`/`createdAt`
 * still arrive as `undefined`, since the search API doesn't return those
 * yet (see docs/Architecture.md's "Known gaps").
 */
export interface Product {
  id: number;
  name: string;
  brand?: string;
  servingSize?: string;
  /** ISO timestamp string. */
  createdAt?: string;
  ingredients: Ingredient[];
}

export interface ProductCardProps {
  product: Product;
}

/**
 * Expandable card for a single product. Collapsed, it shows name/brand
 * and a chevron. Expanded, it shows a metadata block plus this product's
 * ingredients as a nested accordion (only one ingredient open at a time —
 * `expandedIngredientId` is owned here and handed down to each
 * IngredientCard as isExpanded/onToggle).
 */
const ProductCard: React.FC<ProductCardProps> = ({ product }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [expandedIngredientId, setExpandedIngredientId] = useState<
    number | null
  >(null);

  const handleToggle = useCallback(() => {
    setIsExpanded((prev) => !prev);
  }, []);

  const formattedDate = product.createdAt
    ? new Date(product.createdAt).toLocaleDateString()
    : null;

  return (
    <View style={styles.card}>
      <Pressable
        style={styles.headerRow}
        onPress={handleToggle}
        accessibilityRole="button"
        accessibilityLabel={`${isExpanded ? 'Collapse' : 'Expand'} ${product.name}`}
        accessibilityState={{ expanded: isExpanded }}
      >
        <View style={styles.headerText}>
          <Text style={styles.name} numberOfLines={2}>
            {product.name}
          </Text>
          <Text style={styles.brand}>
            {product.brand ? `Brand: ${product.brand}` : 'Brand not available'}
          </Text>
        </View>
        <Ionicons
          name={isExpanded ? 'chevron-up' : 'chevron-down'}
          size={20}
          color={colors.brown}
        />
      </Pressable>

      {isExpanded && (
        <View style={styles.expandedSection}>
          <View style={styles.metadataBlock}>
            <View style={styles.metadataRow}>
              <Text style={styles.metadataLabel}>Full Name</Text>
              <Text style={styles.metadataValue}>{product.name}</Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={styles.metadataLabel}>Brand</Text>
              <Text style={styles.metadataValue}>
                {product.brand ?? 'Not available'}
              </Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={styles.metadataLabel}>Serving Size</Text>
              <Text style={styles.metadataValue}>
                {product.servingSize ?? 'Not available'}
              </Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={styles.metadataLabel}>Scanned</Text>
              <Text style={styles.metadataValue}>
                {formattedDate ?? 'Not available'}
              </Text>
            </View>
          </View>

          <View style={styles.ingredientsSection}>
            <Text style={styles.ingredientsTitle}>Ingredients</Text>
            {product.ingredients.length === 0 ? (
              <Text style={styles.emptyIngredientsText}>
                No ingredient data available for this product yet.
              </Text>
            ) : (
              <View style={styles.ingredientsList}>
                {product.ingredients.map((ingredient) => (
                  <IngredientCard
                    key={ingredient.id}
                    ingredient={ingredient}
                    isExpanded={expandedIngredientId === ingredient.id}
                    onToggle={() =>
                      setExpandedIngredientId((current) =>
                        current === ingredient.id ? null : ingredient.id
                      )
                    }
                  />
                ))}
              </View>
            )}
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
    borderColor: `${colors.olive}66`,
    borderRadius: 12,
    overflow: 'hidden',
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
    padding: spacing.md,
  },
  headerText: {
    flex: 1,
    gap: 2,
  },
  name: {
    fontSize: typography.body,
    fontWeight: '700',
    color: colors.brown,
  },
  brand: {
    fontSize: 13,
    color: colors.brown,
  },
  expandedSection: {
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.md,
    gap: spacing.md,
  },
  metadataBlock: {
    backgroundColor: `${colors.olive}18`,
    borderRadius: 8,
    padding: spacing.sm,
    gap: 4,
  },
  metadataRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  metadataLabel: {
    fontSize: 12,
    fontWeight: '700',
    color: colors.brown,
  },
  metadataValue: {
    fontSize: 12,
    color: colors.brown,
    flexShrink: 1,
    textAlign: 'right',
  },
  ingredientsSection: {
    gap: spacing.sm,
  },
  ingredientsTitle: {
    fontSize: 13,
    fontWeight: '700',
    color: colors.brown,
  },
  ingredientsList: {
    gap: spacing.sm,
  },
  emptyIngredientsText: {
    fontSize: 13,
    fontStyle: 'italic',
    color: `${colors.brown}AA`,
  },
});

export default ProductCard;
