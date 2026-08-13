import React, { useEffect, useState } from 'react';
import { View, Text, FlatList, ActivityIndicator, Pressable, StyleSheet } from 'react-native';
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

  return (
    <View style={styles.screen}>
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

      {isLoading ? (
        <View style={styles.centered}>
          <ActivityIndicator size="large" color={colors.orange} />
        </View>
      ) : errorMessage ? (
        <View style={styles.centered}>
          <Text style={styles.errorText}>{errorMessage}</Text>
        </View>
      ) : (
        <FlatList
          data={results}
          keyExtractor={(item) => `${item.type}-${item.id}`}
          contentContainerStyle={styles.listContent}
          renderItem={({ item }) =>
            item.type === 'product' ? (
              <ProductCard product={toProduct(item)} />
            ) : (
              <IngredientCard
                ingredient={toIngredient(item)}
                isExpanded={expandedIngredientId === item.id}
                onToggle={() =>
                  setExpandedIngredientId((current) =>
                    current === item.id ? null : item.id
                  )
                }
              />
            )
          }
          ListEmptyComponent={
            <View style={styles.centered}>
              <Text style={styles.emptyText}>No results found.</Text>
            </View>
          }
        />
      )}

      <Footer />
    </View>
  );
};

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.offWhite,
  },
  body: {
    paddingVertical: spacing.xl,
    paddingHorizontal: layout.screenHorizontalPadding,
    gap: spacing.md,
  },
  backButton: {
    alignSelf: 'flex-start',
    padding: spacing.xs,
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
  listContent: {
    flexGrow: 1,
    paddingHorizontal: layout.screenHorizontalPadding,
    paddingBottom: spacing.xl,
    gap: spacing.md,
  },
});

export default ResultsScreen;
