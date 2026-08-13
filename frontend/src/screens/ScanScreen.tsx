import React, { useCallback, useState } from 'react';
import {
  View,
  Text,
  Pressable,
  StyleSheet,
  ActivityIndicator,
  Alert,
  ScrollView,
} from 'react-native';

import ImageUploader from '../components/ImageUploader';
import Footer from '../components/Footer';
import { uploadSupplementImage } from '../services/api';
import { colors, layout, spacing, typography } from '../theme';

/**
 * Screen state. `result` is intentionally `unknown` rather than the
 * frontend's `ScanResponse` type: this screen just displays whatever JSON
 * the backend returns (see Results section below), so it isn't coupled to
 * a specific response shape here.
 */
interface ScanScreenState {
  imageUri: string | null;
  isLoading: boolean;
  result: unknown;
}

const ScanScreen: React.FC = () => {
  const [imageUri, setImageUri] = useState<ScanScreenState['imageUri']>(null);
  const [isLoading, setIsLoading] = useState<ScanScreenState['isLoading']>(
    false
  );
  const [result, setResult] = useState<ScanScreenState['result']>(null);

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
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Unknown error occurred.';
      Alert.alert('Upload failed', message);
    } finally {
      setIsLoading(false);
    }
  }, [imageUri, isLoading]);

  const isAnalyzeDisabled: boolean = imageUri === null || isLoading;

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      {/* Centered/padded scan UI lives in its own wrapper so Footer (a
          sibling below it) isn't shrink-wrapped or inset by this
          container's alignItems/padding. */}
      <View style={styles.body}>
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
          <View style={styles.resultsContainer}>
            <Text style={styles.resultsTitle}>Result</Text>
            <ScrollView style={styles.resultsScroll}>
              <Text style={styles.resultsText}>
                {JSON.stringify(result, null, 2)}
              </Text>
            </ScrollView>
          </View>
        )}
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
  // No alignItems/padding here — those live on `body` below. Keeping this
  // container plain (default alignItems: 'stretch', no horizontal inset)
  // is what lets Footer span the full screen width edge-to-edge.
  content: {
    flexGrow: 1,
  },
  body: {
    alignItems: 'center',
    paddingVertical: spacing.xl,
    paddingHorizontal: layout.screenHorizontalPadding,
    gap: spacing.xl,
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
  resultsContainer: {
    width: '100%',
    maxWidth: 420,
    backgroundColor: colors.offWhite,
    borderWidth: 2,
    borderColor: colors.olive,
    borderRadius: 12,
    padding: spacing.md,
  },
  resultsTitle: {
    fontSize: typography.body,
    fontWeight: '700',
    color: colors.brown,
    marginBottom: spacing.sm,
  },
  resultsScroll: {
    maxHeight: 260,
  },
  resultsText: {
    fontFamily: 'monospace',
    fontSize: 13,
    color: colors.brown,
  },
  footerSpacer: {
    flex: 1,
    minHeight: spacing.lg,
  },
});

export default ScanScreen;
