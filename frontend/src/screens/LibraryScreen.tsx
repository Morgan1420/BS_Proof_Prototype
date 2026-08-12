import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  TextInput,
  Pressable,
  StyleSheet,
  ScrollView,
} from 'react-native';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';

import Footer from '../components/Footer';
import { colors, layout, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';
import { fetchSuggestions } from '../services/api';

type LibraryScreenNavigationProp = NativeStackNavigationProp<
  RootStackParamList,
  'LibraryScreen'
>;

// Only fetch suggestions once the user has typed more than this many
// characters. The backend also enforces its own (lower) minimum, so this
// is purely about not firing a request on every early keystroke.
const MIN_SUGGEST_LENGTH = 3;
const SUGGESTION_DEBOUNCE_MS = 300;

/**
 * Supplement Library: a Search section with live autocomplete, and an
 * Explore section with Products/Ingredients browse cards. The NavBar is
 * already rendered above this screen by src/App.tsx (it's persistent
 * across the whole app), so this only needs its own content + Footer.
 */
const LibraryScreen: React.FC = () => {
  const navigation = useNavigation<LibraryScreenNavigationProp>();

  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [suggestionsVisible, setSuggestionsVisible] = useState(false);

  // Debounced live-suggestion fetch. `requestId` guards against a slower
  // earlier request overwriting a faster later one.
  const requestIdRef = useRef(0);

  useEffect(() => {
    if (query.trim().length <= MIN_SUGGEST_LENGTH) {
      setSuggestions([]);
      return;
    }

    const currentRequestId = ++requestIdRef.current;
    const timeoutId = setTimeout(() => {
      fetchSuggestions(query.trim(), 5)
        .then((response) => {
          if (requestIdRef.current === currentRequestId) {
            setSuggestions(response.suggestions);
          }
        })
        .catch((error) => {
          // Live-typeahead failures shouldn't interrupt the user with an
          // alert — just drop the dropdown and log for debugging.
          if (requestIdRef.current === currentRequestId) {
            setSuggestions([]);
          }
          console.warn('Failed to fetch suggestions:', error);
        });
    }, SUGGESTION_DEBOUNCE_MS);

    return () => clearTimeout(timeoutId);
  }, [query]);

  const handleSubmitSearch = useCallback((): void => {
    const trimmed = query.trim();
    if (!trimmed) {
      return;
    }
    setSuggestionsVisible(false);
    navigation.navigate('ResultsScreen', { query: trimmed, filterType: 'all' });
  }, [query, navigation]);

  const handleSuggestionPress = useCallback(
    (suggestion: string): void => {
      setSuggestionsVisible(false);
      setQuery(suggestion);
      navigation.navigate('ResultsScreen', {
        query: suggestion,
        filterType: 'all',
      });
    },
    [navigation]
  );

  const handleBrowseProducts = useCallback((): void => {
    navigation.navigate('ResultsScreen', { filterType: 'products' });
  }, [navigation]);

  const handleBrowseIngredients = useCallback((): void => {
    navigation.navigate('ResultsScreen', { filterType: 'ingredients' });
  }, [navigation]);

  const showDropdown = suggestionsVisible && suggestions.length > 0;

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.contentContainer}
      keyboardShouldPersistTaps="handled"
    >
      <View style={styles.body}>
        {/* SEARCH */}
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Search</Text>
          <Text style={styles.sectionSubtitle}>
            Search our database by product name or active ingredient to
            review dosages, active compounds, and verified label details.
          </Text>

          <View style={styles.searchBarWrapper}>
            <View style={styles.searchBar}>
              <Text style={styles.searchIcon}>{'\u{1F50D}'}</Text>
              <TextInput
                style={styles.searchInput}
                placeholder="Search products or ingredients..."
                placeholderTextColor={`${colors.brown}88`}
                value={query}
                onChangeText={setQuery}
                onFocus={() => setSuggestionsVisible(true)}
                onBlur={() => {
                  // Delay so a suggestion Pressable's onPress still fires
                  // before the dropdown unmounts.
                  setTimeout(() => setSuggestionsVisible(false), 150);
                }}
                onSubmitEditing={handleSubmitSearch}
                returnKeyType="search"
                accessibilityLabel="Search products or ingredients"
              />
              <Pressable
                style={styles.searchButton}
                onPress={handleSubmitSearch}
                accessibilityRole="button"
                accessibilityLabel="Search"
              >
                <Text style={styles.searchButtonText}>Search</Text>
              </Pressable>
            </View>

            {showDropdown && (
              <View style={styles.suggestionsDropdown}>
                {suggestions.map((suggestion) => (
                  <Pressable
                    key={suggestion}
                    style={styles.suggestionItem}
                    onPress={() => handleSuggestionPress(suggestion)}
                    accessibilityRole="button"
                    accessibilityLabel={`Search for ${suggestion}`}
                  >
                    <Text style={styles.suggestionText}>{suggestion}</Text>
                  </Pressable>
                ))}
              </View>
            )}
          </View>
        </View>

        {/* EXPLORE */}
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Explore</Text>
          <Text style={styles.sectionSubtitle}>
            Browse our comprehensive database freely by category to explore
            all indexed products and standalone ingredients.
          </Text>

          <View style={styles.cardsRow}>
            <Pressable
              style={[styles.card, styles.productsCard]}
              onPress={handleBrowseProducts}
              accessibilityRole="button"
              accessibilityLabel="Browse Products"
            >
              <Text style={styles.productsCardText}>PRODUCTS</Text>
            </Pressable>
            <Pressable
              style={[styles.card, styles.ingredientsCard]}
              onPress={handleBrowseIngredients}
              accessibilityRole="button"
              accessibilityLabel="Browse Ingredients"
            >
              <Text style={styles.ingredientsCardText}>INGREDIENTS</Text>
            </Pressable>
          </View>
        </View>
      </View>

      <View style={styles.footerSpacer} />
      <Footer />
    </ScrollView>
  );
};

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: colors.offWhite,
  },
  // No horizontal padding / alignItems here — Footer is a direct child of
  // this container so it can stretch full width (see ScanScreen for the
  // same fix applied there).
  contentContainer: {
    flexGrow: 1,
  },
  body: {
    paddingVertical: spacing.lg,
    paddingHorizontal: layout.screenHorizontalPadding,
    gap: spacing.xl,
  },
  section: {
    gap: spacing.sm,
  },
  sectionTitle: {
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
  },
  sectionSubtitle: {
    fontSize: typography.body,
    color: colors.brown,
    lineHeight: 22,
    marginBottom: spacing.sm,
  },
  searchBarWrapper: {
    position: 'relative',
    zIndex: 10,
  },
  searchBar: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.offWhite,
    borderRadius: 25,
    borderWidth: 1,
    borderColor: `${colors.brown}55`,
    paddingLeft: spacing.md,
    paddingRight: spacing.xs,
    paddingVertical: spacing.xs,
  },
  searchIcon: {
    fontSize: 16,
    marginRight: spacing.sm,
  },
  searchInput: {
    flex: 1,
    fontSize: typography.body,
    color: colors.brown,
    paddingVertical: spacing.sm,
  },
  searchButton: {
    backgroundColor: colors.orange,
    borderRadius: 20,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
  },
  searchButtonText: {
    color: colors.offWhite,
    fontSize: typography.body,
    fontWeight: '700',
  },
  suggestionsDropdown: {
    position: 'absolute',
    top: '100%',
    left: 0,
    right: 0,
    marginTop: spacing.xs,
    backgroundColor: colors.offWhite,
    borderRadius: 14,
    borderWidth: 1,
    borderColor: `${colors.brown}33`,
    overflow: 'hidden',
    zIndex: 20,
    elevation: 6, // Android needs elevation (not just zIndex) to stack above siblings.
  },
  suggestionItem: {
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: `${colors.brown}22`,
  },
  suggestionText: {
    fontSize: typography.body,
    color: colors.brown,
  },
  cardsRow: {
    flexDirection: 'row',
    gap: spacing.md,
  },
  card: {
    flex: 1,
    minHeight: 100,
    borderRadius: 14,
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: spacing.lg,
    paddingHorizontal: spacing.sm,
  },
  productsCard: {
    backgroundColor: colors.orange,
  },
  productsCardText: {
    color: colors.offWhite,
    fontSize: typography.buttonLabel,
    fontWeight: '800',
    letterSpacing: 1,
  },
  ingredientsCard: {
    backgroundColor: colors.yellow,
  },
  ingredientsCardText: {
    color: colors.brown,
    fontSize: typography.buttonLabel,
    fontWeight: '800',
    letterSpacing: 1,
  },
  footerSpacer: {
    minHeight: spacing.lg,
  },
});

export default LibraryScreen;
