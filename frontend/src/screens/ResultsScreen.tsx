import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  FlatList,
  ActivityIndicator,
  Pressable,
  StyleSheet,
} from 'react-native';
import { useNavigation, useRoute } from '@react-navigation/native';
import type { RouteProp } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { Ionicons } from '@expo/vector-icons';

import Footer from '../components/Footer';
import ProductCard from '../components/ProductCard';
import type { Product } from '../components/ProductCard';
import IngredientCard from '../components/IngredientCard';
import type { Ingredient } from '../components/IngredientCard';
import { colors, layout, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';
import { searchSupplements } from '../services/api';
import type { LinkedIngredient, SearchResultItem } from '../services/api';
import { animateCardToggle } from '../utils/animations';

type ResultsScreenRouteProp = RouteProp<RootStackParamList, 'ResultsScreen'>;
type ResultsScreenNavigationProp = NativeStackNavigationProp<
  RootStackParamList,
  'ResultsScreen'
>;

/** Builds the header line describing what's being shown. */
function getHeaderText(
  query: string | undefined,
  filterType: 'all' | 'products' | 'ingredients' | undefined
): string {
  const trimmedQuery = query?.trim();
  if (trimmedQuery) {
    return `Results for "${trimmedQuery}"`;
  }
  if (filterType === 'products') {
    return 'All Products';
  }
  if (filterType === 'ingredients') {
    return 'All Ingredients';
  }
  return 'All Results';
}

/**
 * Maps one backend LinkedIngredient (a ProductIngredientLink + its
 * Ingredient, joined server-side — see
 * app/services/search.py::get_linked_ingredients) onto the Ingredient
 * shape IngredientCard expects for a product-nested row.
 */
function toLinkedIngredient(item: LinkedIngredient): Ingredient {
  return {
    id: item.id,
    name: item.name,
    amount: item.amount ?? undefined,
    unit: item.unit ?? undefined,
    dailyValue: item.daily_value_percentage ?? undefined,
    recommendedDailyDosage: item.recommended_daily_dosage ?? undefined,
    scientificData: item.scientific_data ?? undefined,
  };
}

/**
 * Maps a flat SearchResultItem (type: 'product') onto the richer Product
 * shape ProductCard expects.
 *
 * `ingredients` now comes straight from the backend's nested
 * `SearchResultItem.ingredients` (populated via an explicit join — see
 * get_linked_ingredients) instead of being hardcoded to `[]`; that
 * hardcoding was the root cause of ProductCard always showing "No
 * ingredient data available for this product yet" even for products
 * with real, correctly-persisted ProductIngredientLink rows.
 *
 * Known gap: GET /api/v1/supplements/search still doesn't return serving
 * size or a scan date — only POST /api/v1/scan's response has that
 * detail, and it isn't persisted back out through search. Those two
 * fields render as "Not available" until a serving_size column exists on
 * Product (see docs/Architecture.md's "Known gaps").
 */
function toProduct(item: SearchResultItem): Product {
  return {
    id: item.id,
    name: item.name,
    brand: item.brand ?? undefined,
    servingSize: undefined,
    createdAt: undefined,
    ingredients: (item.ingredients ?? []).map(toLinkedIngredient),
    // No real grading system on the backend yet — every product starts
    // ungraded; see ProductCard.tsx's `handleGradeRequest` for the
    // placeholder-only "assign a grade" interaction.
    is_graded: false,
  };
}

/**
 * Maps a flat SearchResultItem (type: 'ingredient') onto the Ingredient
 * shape IngredientCard expects. As of the backend's Many-to-Many schema
 * refactor, a standalone ingredient search result has no single
 * product's dosage to show (Ingredient is now canonical/shared data) —
 * so this populates the canonical-metadata fields (recommendedDailyDosage/
 * scientificData/productCount) rather than amount/unit/dailyValue, which
 * IngredientCard falls back to displaying instead.
 */
function toIngredient(item: SearchResultItem): Ingredient {
  return {
    id: item.id,
    name: item.name,
    recommendedDailyDosage: item.recommended_daily_dosage ?? undefined,
    scientificData: item.scientific_data ?? undefined,
    productCount: item.product_count ?? undefined,
    // No real grading system on the backend yet — every standalone
    // ingredient result starts ungraded; see IngredientCard.tsx's
    // `handleGradeRequest` for the placeholder-only "assign a grade"
    // interaction (standalone variant only).
    is_graded: false,
  };
}

/**
 * Displays up to 20 search or browse results from
 * GET /api/v1/supplements/search, based on the `query` / `filterType`
 * route params set by LibraryScreen. Product results render as
 * expandable ProductCards; standalone ingredient results (not nested
 * under a product) render as IngredientCards sharing a single-expansion
 * accordion at this screen's level. The NavBar is already rendered above
 * this screen by src/App.tsx, so this only needs its own content + Footer.
 */
const ResultsScreen: React.FC = () => {
  const navigation = useNavigation<ResultsScreenNavigationProp>();
  const route = useRoute<ResultsScreenRouteProp>();
  const { query, filterType } = route.params ?? {};

  const [results, setResults] = useState<SearchResultItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Single-expansion accordion for standalone ingredient rows in this
  // list (ingredients nested inside a ProductCard have their own,
  // separate accordion state owned by that ProductCard).
  const [expandedIngredientId, setExpandedIngredientId] = useState<
    number | null
  >(null);

  // Single-expansion accordion for top-level ProductCard rows — tracked
  // here (rather than inside ProductCard) so expanding one product
  // always collapses any other that was already open.
  const [expandedProductId, setExpandedProductId] = useState<number | null>(
    null
  );

  const flatListRef = useRef<FlatList<SearchResultItem>>(null);

  /**
   * Scrolls the list so row `index` (a top-level ProductCard or
   * IngredientCard that was just expanded) is aligned to the top of the
   * visible area. Deferred one frame via requestAnimationFrame so it
   * doesn't fight the in-flight LayoutAnimation the card's own expand
   * triggered (see src/utils/animations.ts) — scrollToIndex only needs
   * the item's current (pre-expand) offset, which is already known, but
   * nudging it a frame later keeps the two animations from stepping on
   * each other.
   */
  const scrollToItemIndex = useCallback((index: number): void => {
    requestAnimationFrame(() => {
      flatListRef.current?.scrollToIndex({
        index,
        animated: true,
        viewPosition: 0,
      });
    });
  }, []);

  useEffect(() => {
    let cancelled = false;

    setIsLoading(true);
    setErrorMessage(null);

    searchSupplements({ query, filterType, limit: 20 })
      .then((response) => {
        if (!cancelled) {
          setResults(response.results);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          const message =
            error instanceof Error ? error.message : 'Unknown error occurred.';
          setErrorMessage(message);
          setResults([]);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [query, filterType]);

  const headerText = getHeaderText(query, filterType);

  const handleGoBack = (): void => {
    if (navigation.canGoBack()) {
      navigation.goBack();
    }
  };

  // Rendered as the FlatList's own ListHeaderComponent (below) rather
  // than a sibling View above it — see that prop's usage for why: a
  // sibling would sit permanently pinned above the scrollable area
  // (effectively acting sticky even without any explicit `position:
  // sticky`/`fixed` styling), whereas a ListHeaderComponent is genuinely
  // part of the list's own scrollable content and scrolls away with
  // everything else, per the "page headers scroll naturally with
  // content" layout requirement.
  const listHeader = (
    <View style={styles.body}>
      <Pressable
        style={styles.backButton}
        onPress={handleGoBack}
        accessibilityRole="button"
        accessibilityLabel="Go back"
        hitSlop={8}
      >
        <Ionicons name="arrow-back" size={22} color={colors.brown} />
      </Pressable>

      <View style={styles.titleRow}>
        <Text style={styles.headerTitle}>{headerText}</Text>
        {/* Visual placeholder only — not wired to any filtering
            behavior yet. */}
        <Ionicons
          name="filter"
          size={20}
          color={colors.brown}
          accessibilityLabel="Filter (coming soon)"
        />
      </View>
    </View>
  );

  return (
    <View style={styles.screen}>
      {isLoading ? (
        <>
          {listHeader}
          <View style={styles.centered}>
            <ActivityIndicator size="large" color={colors.orange} />
          </View>
          <Footer />
        </>
      ) : errorMessage ? (
        <>
          {listHeader}
          <View style={styles.centered}>
            <Text style={styles.errorText}>{errorMessage}</Text>
          </View>
          <Footer />
        </>
      ) : (
        <FlatList
          ref={flatListRef}
          // `flex: 1` lets this list claim the remaining vertical space
          // in `screen`'s flex column; paired with `contentContainerStyle`'s
          // `flexGrow: 1` below, a short results list still stretches to
          // fill that space so Footer (now `ListFooterComponent`, genuine
          // list content — see below) anchors at the viewport bottom
          // instead of floating right under a couple of cards. On a tall
          // results list this has no effect beyond the normal scrollable
          // behavior — Footer simply becomes visible once the user
          // scrolls past the last card, same as every other screen.
          style={styles.flatList}
          data={results}
          keyExtractor={(item) => `${item.type}-${item.id}`}
          ListHeaderComponent={listHeader}
          // Footer is rendered as genuine list content here — not as a
          // sibling next to the FlatList — specifically so it can never
          // end up in a separate, independently-sized flex box from the
          // cards above it (which is what let it visually overlap/cut
          // off the last card before this fix: a `flex: 1` FlatList
          // sitting next to a `flex: 1` Footer.tsx creates a second,
          // FlatList-bounded scroll region distinct from the page's own
          // flow, so Footer — a normal-height sibling right after it —
          // could end up pinned at the bottom of that bounded region
          // regardless of whether the user had actually scrolled the
          // list to its end). As `ListFooterComponent`, Footer is simply
          // the last row in the one true scrollable list, guaranteed to
          // render immediately after the last card with no independent
          // sizing of its own — it becomes visible if and only if the
          // user has scrolled past everything above it. See `listContent`
          // below for why this doesn't compromise Footer's usual
          // full-bleed (edge-to-edge, unpadded) width.
          ListFooterComponent={<Footer />}
          contentContainerStyle={styles.listContent}
          renderItem={({ item, index }) => (
            <View style={styles.itemWrapper}>
              {item.type === 'product' ? (
                <ProductCard
                  product={toProduct(item)}
                  isExpanded={expandedProductId === item.id}
                  onToggle={() => {
                    animateCardToggle();
                    setExpandedProductId((current) => {
                      const next = current === item.id ? null : item.id;
                      if (next !== null) {
                        scrollToItemIndex(index);
                      }
                      return next;
                    });
                  }}
                  onNestedIngredientExpand={() => scrollToItemIndex(index)}
                />
              ) : (
                <IngredientCard
                  ingredient={toIngredient(item)}
                  variant="standalone"
                  isExpanded={expandedIngredientId === item.id}
                  onToggle={() => {
                    animateCardToggle();
                    setExpandedIngredientId((current) => {
                      const next = current === item.id ? null : item.id;
                      if (next !== null) {
                        scrollToItemIndex(index);
                      }
                      return next;
                    });
                  }}
                />
              )}
            </View>
          )}
          // scrollToIndex needs the target row already measured; this is
          // a defensive fallback (item not yet laid out) rather than the
          // expected path — the tapped card is always already on screen.
          onScrollToIndexFailed={(info) => {
            setTimeout(() => {
              flatListRef.current?.scrollToIndex({
                index: info.index,
                animated: true,
                viewPosition: 0,
              });
            }, 100);
          }}
          ListEmptyComponent={
            <View style={styles.centered}>
              <Text style={styles.emptyText}>No results found.</Text>
            </View>
          }
        />
      )}
    </View>
  );
};

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.offWhite,
  },
  // See the inline comment where this is applied — lets the list claim
  // remaining vertical space so Footer pins to the bottom of the
  // viewport rather than riding up under a short results list.
  flatList: {
    flex: 1,
  },
  // The screen's *only* source of horizontal inset for the header —
  // `listContent` below deliberately does NOT also set
  // `paddingHorizontal` (a previous pass had it on both, which double-
  // padded the header: once here, once again from `listContent`, since
  // this whole `body` View is rendered as the FlatList's
  // `ListHeaderComponent` and therefore a child of that padded
  // container — that's what made the header render as a narrow,
  // centered box next to full-width cards). `itemWrapper` below applies
  // this exact same `layout.screenHorizontalPadding` token to each card
  // instead, so the header and every card share one single, unduplicated
  // inset — guaranteed identical left/right edges without a hardcoded
  // `maxWidth` to keep in sync between the two.
  body: {
    paddingVertical: spacing.xl,
    paddingHorizontal: layout.screenHorizontalPadding,
    gap: spacing.md,
  },
  // No `padding` here (previously `padding: spacing.xs`, inherited from
  // this screen's very first version) — that extra padding shifted the
  // back arrow's actual visual edge inward by 4px past `body`'s own
  // horizontal inset, so it sat slightly to the right of where a card's
  // left border lines up below it. `hitSlop` alone still gives the
  // button a comfortable tap target, without moving its rendered
  // position — so the arrow's icon glyph now sits flush with the card
  // grid's left edge, matching the filter icon (bare `Ionicons`, no
  // padding of its own) on the right, which was already flush.
  backButton: {
    alignSelf: 'flex-start',
  },
  titleRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBottom: spacing.sm,
  },
  headerTitle: {
    flex: 1,
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  centered: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.lg,
  },
  errorText: {
    fontSize: typography.body,
    color: colors.brown,
    textAlign: 'center',
  },
  emptyText: {
    fontSize: typography.body,
    color: colors.brown,
    textAlign: 'center',
  },
  // No `paddingHorizontal` here — see `body`'s own comment above for
  // why: this is the FlatList's `contentContainerStyle`, the shared
  // parent of the header, every card (via `itemWrapper` below), AND now
  // `Footer` (as `ListFooterComponent`). Padding it here would inset all
  // three uniformly, including Footer — which is supposed to stay
  // full-bleed edge-to-edge, same as it is on every other screen (see
  // theme.ts's `layout.screenHorizontalPadding` docstring: "deliberately
  // NOT applied to NavBar, Footer, ..."). `gap` still applies (a flex
  // `gap` is independent of horizontal padding) — it's what puts a
  // little breathing room between the header, each card, and Footer.
  listContent: {
    flexGrow: 1,
    gap: spacing.md,
  },
  // Applied per-card (not on `listContent`, which stays unpadded so
  // Footer can render full-bleed — see that style's comment) — the same
  // `layout.screenHorizontalPadding` token `body` uses for the header,
  // so every card lines up with the header's back arrow/filter icon
  // exactly, from one single inset source.
  itemWrapper: {
    paddingHorizontal: layout.screenHorizontalPadding,
  },
});

export default ResultsScreen;
