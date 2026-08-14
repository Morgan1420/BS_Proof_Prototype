import React, { useCallback, useRef, useState } from 'react';
import { View, Text, Pressable, StyleSheet, Platform } from 'react-native';
import { Ionicons } from '@expo/vector-icons';

import IngredientCard from './IngredientCard';
import type { Ingredient } from './IngredientCard';
import GradeBadge, { PLACEHOLDER_GRADE_VALUE } from './GradeBadge';
import { colors, spacing, typography } from '../theme';
import { animateCardToggle } from '../utils/animations';

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
  /** Whether this product already has a grade. There's no real grading
   * system on the backend yet, so every mapping function that builds a
   * `Product` (ResultsScreen's `toProduct`, ScanScreen's
   * `toScannedProduct`) initializes this to `false` — see GradeBadge.tsx
   * and this component's own `handleGradeRequest` below for the
   * placeholder-only "assign a grade" interaction. */
  is_graded?: boolean;
}

export interface ProductCardProps {
  product: Product;
  /** Whether this card is currently expanded. Controlled by the parent
   * (ResultsScreen tracks one `expandedProductId` across the whole list;
   * ScanScreen tracks its own single boolean) so only one ProductCard in
   * a group can be open at once — mirrors IngredientCardProps exactly. */
  isExpanded: boolean;
  /** Called when the header is tapped; the parent decides what "open"
   * means (usually: toggle this id, closing any other open sibling). */
  onToggle: () => void;
  /** Notified whenever a nested IngredientCard inside this product
   * expands, so the parent can re-align its own scroll position (e.g.
   * re-run the same top-level scrollToIndex it uses on this
   * ProductCard's own expansion) to keep the newly revealed content from
   * being clipped. Only relevant on native — on web, ProductCard handles
   * this itself directly via the tapped ingredient's own
   * `scrollIntoView`, since a plain DOM ref is all that's needed there
   * (see handleIngredientToggle below). Optional: harmless to omit,
   * mobile just won't auto-scroll for nested expansion. */
  onNestedIngredientExpand?: () => void;
}

/**
 * Expandable card for a single product. Collapsed, it shows name/brand
 * and a chevron. Expanded, it shows a metadata block plus this product's
 * ingredients as a nested accordion (only one ingredient open at a time —
 * `expandedIngredientId` is owned here and handed down to each
 * IngredientCard as isExpanded/onToggle).
 */
const ProductCard: React.FC<ProductCardProps> = ({
  product,
  isExpanded,
  onToggle,
  onNestedIngredientExpand,
}) => {
  const [expandedIngredientId, setExpandedIngredientId] = useState<
    number | null
  >(null);

  // Local, placeholder-only "graded" state — there's no real grading
  // system on the backend yet (see GradeBadge.tsx), so this just tracks
  // whether the header's grade pill has been "assigned" in this session.
  // Initialized from `product.is_graded` (currently always `false` from
  // every mapping function) but not re-synced if the `product` prop
  // changes identity later — same pattern as `isProductExpanded` on
  // ScanScreen, acceptable for a placeholder interaction.
  const [isGraded, setIsGraded] = useState(product.is_graded ?? false);

  const handleGradeRequest = useCallback(() => {
    animateCardToggle();
    setIsGraded(true);
  }, []);

  // One ref per rendered ingredient row, keyed by ingredient id —
  // forwarded straight through to IngredientCard's own outer View (see
  // IngredientCard.tsx). Used on web only, to scroll a just-expanded
  // ingredient into view via the DOM's native `scrollIntoView` (React
  // Native Web forwards View refs to the underlying DOM node). Only the
  // currently-relevant ref is ever read; stale entries from ingredients
  // that unmounted/re-rendered are harmless since they're overwritten via
  // the callback ref below every render.
  const ingredientRowRefs = useRef<Record<number, View | null>>({});

  const handleToggle = useCallback(() => {
    animateCardToggle();
    onToggle();
  }, [onToggle]);

  const handleIngredientToggle = useCallback(
    (ingredientId: number) => {
      animateCardToggle();
      setExpandedIngredientId((current) => {
        const next = current === ingredientId ? null : ingredientId;
        if (next !== null) {
          // Deferred a frame so this runs after the layout pass that
          // reflects the state update just made, rather than racing the
          // in-flight LayoutAnimation.
          requestAnimationFrame(() => {
            if (Platform.OS === 'web') {
              // React Native Web forwards `View` refs to the underlying
              // DOM node, which supports `scrollIntoView` natively — no
              // native node handles, no parent coordination needed.
              const rowNode = ingredientRowRefs.current[ingredientId] as
                | (View & {
                    scrollIntoView?: (options?: {
                      behavior?: 'auto' | 'smooth';
                      block?: 'start' | 'center' | 'end' | 'nearest';
                    }) => void;
                  })
                | null;
              if (rowNode && typeof rowNode.scrollIntoView === 'function') {
                rowNode.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
              }
            } else {
              // Native: no cross-platform way to measure a nested row's
              // offset without native node handles, so fall back to
              // re-running the parent's own top-level scroll (the same
              // one triggered when this ProductCard itself expanded) —
              // it re-pins the card's top to the viewport top, which
              // maximizes the room left below to show the newly
              // revealed content.
              onNestedIngredientExpand?.();
            }
          });
        }
        return next;
      });
    },
    [onNestedIngredientExpand]
  );

  const formattedDate = product.createdAt
    ? new Date(product.createdAt).toLocaleDateString()
    : null;

  return (
    <View style={[styles.card, isExpanded && styles.cardExpanded]}>
      <Pressable
        style={styles.headerRow}
        onPress={handleToggle}
        accessibilityRole="button"
        accessibilityLabel={`${isExpanded ? 'Collapse' : 'Expand'} ${product.name}`}
        accessibilityState={{ expanded: isExpanded }}
      >
        <View style={styles.headerText}>
          <Text
            style={[styles.name, isExpanded && styles.expandedTextColor]}
            numberOfLines={2}
          >
            {product.name}
          </Text>
          <Text style={[styles.brand, isExpanded && styles.expandedTextColor]}>
            {product.brand ? `Brand: ${product.brand}` : 'Brand not available'}
          </Text>
        </View>
        <View style={styles.headerRight}>
          <GradeBadge
            isGraded={isGraded}
            onRequestGrade={handleGradeRequest}
            isExpanded={isExpanded}
            gradeValue={PLACEHOLDER_GRADE_VALUE}
          />
          <Ionicons
            name={isExpanded ? 'chevron-up' : 'chevron-down'}
            size={20}
            color={colors.brown}
          />
        </View>
      </Pressable>

      {isExpanded && (
        <View style={styles.expandedSection}>
          <View style={styles.metadataBlock}>
            <View style={styles.metadataRow}>
              <Text style={[styles.metadataLabel, styles.expandedTextColor]}>
                Full Name
              </Text>
              <Text style={[styles.metadataValue, styles.expandedTextColor]}>
                {product.name}
              </Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={[styles.metadataLabel, styles.expandedTextColor]}>
                Brand
              </Text>
              <Text style={[styles.metadataValue, styles.expandedTextColor]}>
                {product.brand ?? 'Not available'}
              </Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={[styles.metadataLabel, styles.expandedTextColor]}>
                Serving Size
              </Text>
              <Text style={[styles.metadataValue, styles.expandedTextColor]}>
                {product.servingSize ?? 'Not available'}
              </Text>
            </View>
            <View style={styles.metadataRow}>
              <Text style={[styles.metadataLabel, styles.expandedTextColor]}>
                Scanned
              </Text>
              <Text style={[styles.metadataValue, styles.expandedTextColor]}>
                {formattedDate ?? 'Not available'}
              </Text>
            </View>
          </View>

          <View style={styles.ingredientsSection}>
            <Text style={[styles.ingredientsTitle, styles.expandedTextColor]}>
              Ingredients
            </Text>
            {product.ingredients.length === 0 ? (
              <Text
                style={[styles.emptyIngredientsText, styles.expandedTextColor]}
              >
                No ingredient data available for this product yet.
              </Text>
            ) : (
              <View style={styles.ingredientsList}>
                {product.ingredients.map((ingredient) => (
                  <IngredientCard
                    key={ingredient.id}
                    ref={(el) => {
                      ingredientRowRefs.current[ingredient.id] = el;
                    }}
                    ingredient={ingredient}
                    isExpanded={expandedIngredientId === ingredient.id}
                    onToggle={() => handleIngredientToggle(ingredient.id)}
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
    // Thicker, dark-green-by-default border (was a thin, translucent
    // olive one) — overridden by cardExpanded (below) while open.
    borderWidth: 3,
    borderColor: colors.darkGreen,
    // Rounder, more modern feel — up from 12.
    borderRadius: 20,
    overflow: 'hidden',
  },
  // Applied on top of `card` (via a conditional array style) while the
  // product is expanded — orange accent border, per spec.
  cardExpanded: {
    borderColor: colors.orange,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: spacing.sm,
    // Bumped from spacing.md (16) for a roomier, more generous card.
    padding: spacing.lg,
  },
  headerText: {
    flex: 1,
    gap: spacing.xs,
  },
  // Groups the grade badge + chevron together on the header row's right
  // side, same pattern as standalone IngredientCard's
  // `standaloneHeaderRight` — keeps the row's two-item space-between
  // layout intact (name group left, badge+chevron group right).
  headerRight: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
  },
  name: {
    fontSize: typography.resultCardTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  brand: {
    fontSize: typography.resultCardTag,
    color: colors.brown,
  },
  // Applied on top of every other text style (via a conditional array
  // style, e.g. `[styles.name, isExpanded && styles.expandedTextColor]`)
  // while this card is expanded — forces every text element inside it
  // (name, brand, metadata labels/values, ingredients title, empty-state
  // text) to the palette orange, per spec. The card's own background/
  // border colors are untouched by this — only text.
  expandedTextColor: {
    color: colors.orange,
  },
  expandedSection: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    gap: spacing.lg,
  },
  metadataBlock: {
    backgroundColor: `${colors.olive}18`,
    borderRadius: 8,
    padding: spacing.md,
    gap: spacing.xs,
  },
  metadataRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: spacing.sm,
  },
  metadataLabel: {
    fontSize: typography.resultCardLabel,
    fontWeight: '700',
    color: colors.brown,
  },
  metadataValue: {
    fontSize: typography.resultCardLabel,
    color: colors.brown,
    flexShrink: 1,
    textAlign: 'right',
  },
  ingredientsSection: {
    gap: spacing.md,
  },
  // Bigger/bolder than the surrounding card text so it reads as a clear
  // section divider between the metadata block above and the ingredient
  // list below (was resultCardTag/15/700 — too close in weight to the
  // body text around it to stand out as its own section).
  ingredientsTitle: {
    fontSize: typography.sectionTitle,
    fontWeight: '800',
    color: colors.brown,
  },
  ingredientsList: {
    gap: spacing.md,
  },
  emptyIngredientsText: {
    fontSize: typography.resultCardLabel,
    fontStyle: 'italic',
    color: `${colors.brown}AA`,
  },
});

export default ProductCard;
