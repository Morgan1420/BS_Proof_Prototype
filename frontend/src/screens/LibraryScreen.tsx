import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  View,
  Text,
  TextInput,
  Pressable,
  StyleSheet,
  ScrollView,
  ImageBackground,
  Platform,
  useWindowDimensions,
} from 'react-native';
import type { TextStyle } from 'react-native';
import { useNavigation } from '@react-navigation/native';
import type { NativeStackNavigationProp } from '@react-navigation/native-stack';
import { Ionicons } from '@expo/vector-icons';

import Footer from '../components/Footer';
import { colors, layout, spacing, typography } from '../theme';
import type { RootStackParamList } from '../navigation/types';
import { fetchSuggestions } from '../services/api';

const PRODUCTS_IMAGE = require('../assets/products.png');
const INGREDIENTS_IMAGE = require('../assets/ingredients.png');

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
  const { width: windowWidth } = useWindowDimensions();

  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [suggestionsVisible, setSuggestionsVisible] = useState(false);
  // Drives the search bar's orange focus styling (border + button
  // background) — separate from `suggestionsVisible`, which controls the
  // autocomplete dropdown and has its own blur-delay timing (see onBlur
  // below).
  const [isFocused, setIsFocused] = useState(false);

  // 15% of the actual screen width (not the row's, which is narrower due
  // to the 20% horizontal screen inset) for the Products/Ingredients
  // cards below — per spec, computed via useWindowDimensions() (reactive
  // to resize/rotation) rather than a one-off Dimensions.get() call.
  const exploreCardSize = windowWidth * 0.15;

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
            <View
              style={[styles.searchBar, isFocused && styles.searchBarFocused]}
            >
              <TextInput
                style={styles.searchInput}
                placeholder="Search products or ingredients..."
                placeholderTextColor={`${colors.brown}88`}
                selectionColor={colors.darkGreen}
                underlineColorAndroid="transparent"
                value={query}
                onChangeText={setQuery}
                onFocus={() => {
                  setIsFocused(true);
                  setSuggestionsVisible(true);
                }}
                onBlur={() => {
                  setIsFocused(false);
                  // Delay so a suggestion Pressable's onPress still fires
                  // before the dropdown unmounts.
                  setTimeout(() => setSuggestionsVisible(false), 150);
                }}
                onSubmitEditing={handleSubmitSearch}
                returnKeyType="search"
                accessibilityLabel="Search products or ingredients"
              />
              <Pressable
                style={[
                  styles.searchButton,
                  isFocused && styles.searchButtonFocused,
                ]}
                onPress={handleSubmitSearch}
                accessibilityRole="button"
                accessibilityLabel="Search"
              >
                <Ionicons name="search" size={22} color={colors.offWhite} />
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
              style={[
                styles.card,
                styles.productsCard,
                { width: exploreCardSize, height: exploreCardSize },
              ]}
              onPress={handleBrowseProducts}
              accessibilityRole="button"
              accessibilityLabel="Browse Products"
            >
              <ImageBackground
                source={PRODUCTS_IMAGE}
                style={styles.cardImage}
                blurRadius={2}
              >
                <View style={styles.cardOverlay} />
                <Text style={styles.cardText}>PRODUCTS</Text>
              </ImageBackground>
            </Pressable>
            <Pressable
              style={[
                styles.card,
                styles.ingredientsCard,
                { width: exploreCardSize, height: exploreCardSize },
              ]}
              onPress={handleBrowseIngredients}
              accessibilityRole="button"
              accessibilityLabel="Browse Ingredients"
            >
              <ImageBackground
                source={INGREDIENTS_IMAGE}
                style={styles.cardImage}
                blurRadius={2}
              >
                <View style={styles.cardOverlay} />
                <Text style={styles.cardText}>INGREDIENTS</Text>
              </ImageBackground>
            </Pressable>
          </View>
        </View>
      </View>

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
  // same fix applied there). `justifyContent: 'space-between'` (with
  // `flexGrow: 1` below) is what pins Footer to the viewport bottom on
  // short content: `body` is pushed to the top edge and `Footer` to the
  // bottom edge of the (at-least-viewport-tall) content container; on
  // tall content it has no effect and Footer just follows normally at
  // the end of the scrollable content, same as before.
  contentContainer: {
    flexGrow: 1,
    justifyContent: 'space-between',
  },
  body: {
    paddingVertical: spacing.xl,
    paddingHorizontal: layout.screenHorizontalPadding,
    // Wider gap between the Search and Explore sections than before.
    gap: spacing.xl * 1.75,
  },
  section: {
    gap: spacing.md,
    // Center headers/subheadings; the search bar and card row below are
    // separate (non-Text) elements so they're unaffected by this and
    // still stretch full-width.
    alignItems: 'center',
  },
  sectionTitle: {
    fontSize: typography.sectionTitleLarge,
    fontWeight: '700',
    color: colors.brown,
    textAlign: 'center',
  },
  sectionSubtitle: {
    fontSize: typography.body,
    color: colors.brown,
    lineHeight: 22,
    marginBottom: spacing.sm,
    textAlign: 'center',
  },
  searchBarWrapper: {
    position: 'relative',
    zIndex: 10,
    // `section`'s alignItems: 'center' (above) would otherwise
    // shrink-wrap this to its content width — stretch it back to full
    // section width explicitly.
    alignSelf: 'stretch',
  },
  searchBar: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.offWhite,
    height: 56,
    borderRadius: 28,
    // Thicker, dark-green outer border (was a thin, translucent-brown
    // one) — matches the search button and NavBar. Overridden by
    // searchBarFocused (below) while the input is focused.
    borderWidth: 3,
    borderColor: colors.darkGreen,
    paddingLeft: spacing.lg,
    paddingRight: 6,
  },
  // Applied on top of `searchBar` (via a conditional array style) while
  // the TextInput is focused — swaps the border to orange.
  searchBarFocused: {
    borderColor: colors.orange,
  },
  searchInput: {
    flex: 1,
    fontSize: typography.body,
    color: colors.brown,
    height: '100%',
    // Browsers draw their own default focus ring/highlight on a focused
    // <input> — react-native-web maps `outlineStyle` straight to CSS
    // `outline`, so `'none'` suppresses it (native TextInput has no such
    // ring to begin with, hence gating on web). @types/react-native
    // already has an `outlineStyle` key, but typed for a *native*
    // border-style meaning ('solid'/'dotted'/'dashed') that doesn't
    // include 'none' — a naming collision with RNW's web-only usage, not
    // a mistake on our end. `as unknown as TextStyle` steps around that
    // stricter, wrong-for-this-context type, same spirit as the
    // textShadow shorthand cast on HomeScreen.
    ...(Platform.OS === 'web'
      ? ({ outlineStyle: 'none' } as unknown as TextStyle)
      : {}),
  },
  // Dedicated circular action button (no inline icon in the input area
  // anymore, no "Search" label) — matches the pill bar's rounded ends.
  // Overridden by searchButtonFocused (below) while the input is focused.
  searchButton: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.darkGreen,
    alignItems: 'center',
    justifyContent: 'center',
  },
  searchButtonFocused: {
    backgroundColor: colors.orange,
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
    justifyContent: 'center',
    // Wider gap now that the cards themselves are smaller and no longer
    // stretch edge-to-edge (was spacing.md/16).
    gap: spacing.xl,
    // Same reason as searchBarWrapper above — stay full-width despite
    // `section`'s centering alignItems, so `justifyContent: 'center'`
    // here has the whole section's width to center the (now
    // fixed-size, not flex:1) cards within.
    alignSelf: 'stretch',
  },
  // Base card shape only — width/height are set inline per-card from
  // `exploreCardSize` (15% of the actual window width, via
  // useWindowDimensions()) rather than fixed here, since a %-based
  // StyleSheet value would resolve against this card's parent
  // (`cardsRow`), not the true screen width the spec asks for.
  // `overflow: hidden` clips the ImageBackground photo to the rounded
  // corners.
  card: {
    borderRadius: 14,
    overflow: 'hidden',
  },
  // No border — both cards now rely entirely on the photo + dark
  // overlay for their look (previously a thin orange/yellow border kept
  // them visually distinct; removed per updated design).
  productsCard: {
    borderWidth: 0,
  },
  ingredientsCard: {
    borderWidth: 0,
  },
  cardImage: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  // Dark tint over the (blurred) photo so the bold label stays crisp
  // and readable regardless of the image underneath. Explicit
  // top/left/right/bottom (matching the same pattern used for Home's
  // Hero overlay) rather than StyleSheet.absoluteFillObject, which
  // isn't available in this RN/@types version.
  cardOverlay: {
    position: 'absolute',
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundColor: 'rgba(0, 0, 0, 0.4)',
  },
  cardText: {
    // Dialed back from typography.sectionTitle (22) now that the cards
    // themselves are smaller (120x120) — keeps "INGREDIENTS" from
    // crowding/wrapping awkwardly inside the shrunk card.
    color: colors.offWhite,
    fontSize: typography.buttonLabel,
    fontWeight: '800',
    letterSpacing: 1,
    textAlign: 'center',
    paddingHorizontal: spacing.xs,
  },
});

export default LibraryScreen;
