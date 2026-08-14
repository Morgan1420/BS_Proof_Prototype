import React, { useCallback, useRef, useState } from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  ActivityIndicator,
  Alert,
  ScrollView,
} from 'react-native';
import type { LayoutChangeEvent } from 'react-native';

import ImageUploader from '../components/ImageUploader';
import Footer from '../components/Footer';
import ProductCard from '../components/ProductCard';
import type { Product } from '../components/ProductCard';
import { uploadSupplementImage } from '../services/api';
import type { SupplementAnalysis } from '../services/api';
import { colors, layout, spacing, typography } from '../theme';
import { animateCardToggle } from '../utils/animations';

/** Screen state. */
interface ScanScreenState {
  imageUri: string | null;
  isLoading: boolean;
  result: SupplementAnalysis | null;
}

/**
 * Maps a POST /api/v1/scan response onto the `Product` shape ProductCard
 * expects, so the just-scanned result can render through the exact same
 * component as ResultsScreen's list items.
 *
 * This is an ephemeral, not-yet-persisted view of the scan: the backend
 * doesn't return the saved Product's id (a documented gap — see
 * docs/Architecture.md's "Known gaps"), and the per-scan ingredient rows
 * (app/schemas/supplement.py::Ingredient) don't carry the canonical
 * Ingredient.id either — both are synthesized here (product id `0`,
 * ingredient id = array index) purely so ProductCard/IngredientCard have
 * something to key/track expansion by. They're not real database ids and
 * shouldn't be used for anything beyond this render.
 */
function toScannedProduct(analysis: SupplementAnalysis): Product {
  return {
    id: 0,
    name: analysis.product_name ?? 'Unnamed product',
    brand: undefined,
    servingSize: analysis.serving_size ?? undefined,
    createdAt: undefined,
    ingredients: analysis.ingredients.map((item, index) => ({
      id: index,
      name: item.name,
      amount: item.amount,
      unit: item.unit,
      dailyValue: item.daily_value ?? undefined,
    })),
    // No real grading system on the backend yet — every freshly-scanned
    // product starts ungraded; see ProductCard.tsx's `handleGradeRequest`
    // for the placeholder-only "assign a grade" interaction.
    is_graded: false,
  };
}

const ScanScreen: React.FC = () => {
  const [imageUri, setImageUri] = useState<ScanScreenState['imageUri']>(null);
  const [isLoading, setIsLoading] = useState<ScanScreenState['isLoading']>(
    false
  );
  const [result, setResult] = useState<ScanScreenState['result']>(null);

  // ProductCard is now a fully controlled component (mirrors
  // ResultsScreen) — ScanScreen only ever shows one product, so this is
  // a plain boolean rather than an id. Starts `true` so a fresh scan
  // result renders already expanded (the whole point of just having
  // scanned something is to immediately see what was found), and is
  // reset to `true` again on every new successful scan below, so a
  // manually-collapsed card from a previous result doesn't carry over.
  const [isProductExpanded, setIsProductExpanded] = useState(true);

  const scrollViewRef = useRef<ScrollView>(null);

  // Tracks resultsContainer's Y offset within the ScrollView's content
  // (it's a direct child of `body`, which is itself the ScrollView's
  // first child, so this single onLayout is all that's needed — no
  // cross-boundary native measurement required). Used as a native
  // (non-web) fallback scroll target when a nested ingredient expands —
  // see handleNestedIngredientExpand below.
  const resultsContainerYRef = useRef(0);
  const handleResultsContainerLayout = useCallback(
    (event: LayoutChangeEvent): void => {
      resultsContainerYRef.current = event.nativeEvent.layout.y;
    },
    []
  );

  /**
   * Native-only fallback for nested ingredient auto-scroll (ProductCard
   * handles web itself via the tapped row's own `scrollIntoView`,
   * bypassing this entirely — see ProductCard.tsx). Since this screen
   * only ever shows one product, this just re-scrolls to the top of the
   * results card, maximizing the space left below to show the newly
   * revealed nested content.
   */
  const handleNestedIngredientExpand = useCallback((): void => {
    scrollViewRef.current?.scrollTo({
      y: resultsContainerYRef.current,
      animated: true,
    });
  }, []);

  const handleImageSelected = useCallback((uri: string | null): void => {
    setImageUri(uri);
    setResult(null);
  }, []);

  /** Sends the selected image to the backend and stores the raw response. */
  const handleAnalyze = useCallback(async (): Promise<void> => {
    if (!imageUri || isLoading) {
      return;
    }

    setIsLoading(true);
    setResult(null);
    try {
      const response = await uploadSupplementImage(imageUri);
      setResult(response);
      setIsProductExpanded(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Unknown error occurred.';
      Alert.alert('Upload failed', message);
    } finally {
      setIsLoading(false);
    }
  }, [imageUri, isLoading]);

  const isAnalyzeDisabled: boolean = imageUri === null || isLoading;

  // Nothing chosen and nothing back from the backend yet — the true idle
  // state. Once an image is picked (even before Analyze is tapped), the
  // layout switches to top-aligned so the soon-to-grow content below
  // (loading spinner, then the ProductCard) doesn't fight a centered
  // container.
  const isEmptyState = imageUri === null && result === null;

  return (
    <ScrollView
      ref={scrollViewRef}
      style={styles.screen}
      contentContainerStyle={styles.content}
    >
      {/* Centered/padded scan UI lives in its own wrapper so Footer (a
          sibling below it) isn't shrink-wrapped or inset by this
          container's alignItems/padding. `body` is `flex: 1` in both
          states — see the `body`/`bodyCentered` styles below for why
          that alone is what both centers the idle state AND still pins
          Footer to the bottom on short content. */}
      <View style={[styles.body, isEmptyState && styles.bodyCentered]}>
        <Text style={styles.title}>Scan Supplement</Text>

        <ImageUploader
          imageUri={imageUri}
          onImageSelected={handleImageSelected}
        />

        <Pressable
          style={[
            styles.analyzeButton,
            isAnalyzeDisabled && styles.analyzeButtonDisabled,
          ]}
          onPress={handleAnalyze}
          disabled={isAnalyzeDisabled}
          accessibilityRole="button"
          accessibilityLabel="Analyze Label"
          accessibilityState={{ disabled: isAnalyzeDisabled, busy: isLoading }}
        >
          {isLoading ? (
            <ActivityIndicator color={colors.offWhite} />
          ) : (
            <Text style={styles.analyzeButtonText}>Analyze Label</Text>
          )}
        </Pressable>

        {result !== null && (
          <View
            style={styles.resultsContainer}
            onLayout={handleResultsContainerLayout}
          >
            <Text style={styles.resultsTitle}>Result</Text>
            {/* Same ProductCard used on ResultsScreen — controlled the
                same way (isExpanded/onToggle), starts expanded (see
                isProductExpanded above) so the just-scanned result is
                immediately visible without an extra tap, and shares the
                same nested-ingredient auto-scroll behavior (web:
                ProductCard scrolls the tapped row into view itself;
                native: falls back to this screen's own scroll-to-top-of-
                result helper). */}
            <ProductCard
              product={toScannedProduct(result)}
              isExpanded={isProductExpanded}
              onToggle={() => {
                animateCardToggle();
                setIsProductExpanded((prev) => !prev);
              }}
              onNestedIngredientExpand={handleNestedIngredientExpand}
            />
          </View>
        )}
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
  // No alignItems/padding here — those live on `body` below. Keeping this
  // container plain (default alignItems: 'stretch', no horizontal inset)
  // is what lets Footer span the full screen width edge-to-edge.
  content: {
    flexGrow: 1,
  },
  // `flex: 1` unconditionally (both states) is what replaces the old
  // separate `footerSpacer` `View` — this single container growing to
  // fill any leftover space in the flexGrow: 1 ScrollView content is
  // enough on its own to push Footer to the bottom on short content.
  // When content is taller than the available space, flex: 1 has no
  // effect (it only grows, never shrinks below content size), so the
  // normal top-aligned/scrollable case is unaffected.
  body: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'flex-start',
    paddingVertical: spacing.xl,
    paddingHorizontal: layout.screenHorizontalPadding,
    gap: spacing.xl,
  },
  // Idle/empty state only (see isEmptyState) — centers the upload card,
  // prompt text, and button vertically within the space `body`'s
  // flex: 1 gives it, rather than sitting top-aligned.
  bodyCentered: {
    justifyContent: 'center',
  },
  title: {
    fontSize: typography.sectionTitle,
    fontWeight: '700',
    color: colors.brown,
    textAlign: 'center',
  },
  analyzeButton: {
    width: 260,
    paddingVertical: spacing.md,
    borderRadius: 12,
    backgroundColor: colors.orange,
    alignItems: 'center',
    justifyContent: 'center',
  },
  // Uses opacity rather than a new color so the disabled state stays
  // within the strict palette (still orange underneath, just muted).
  analyzeButtonDisabled: {
    opacity: 0.5,
  },
  analyzeButtonText: {
    fontSize: typography.buttonLabel,
    fontWeight: '700',
    color: colors.offWhite,
  },
  // Just a layout wrapper now (width/gap for the "Result" label above
  // the card) — no border/background of its own anymore, since
  // ProductCard already has its own (and stacking two borders around
  // each other read as cluttered). No maxWidth cap (removed) so the
  // card stretches to the full width available within `body`'s 20%
  // horizontal padding, matching ResultsScreen's ProductCard sizing
  // exactly rather than an arbitrary, screen-specific 420px cap.
  resultsContainer: {
    width: '100%',
    gap: spacing.sm,
  },
  resultsTitle: {
    fontSize: typography.body,
    fontWeight: '700',
    color: colors.brown,
  },
});

export default ScanScreen;
