import React, { useState, useCallback } from 'react';
import { View, Text, Pressable, StyleSheet, SafeAreaView } from 'react-native';
import ImageUploader from '../components/ImageUploader';

/**
 * Local screen state for the home / upload screen.
 */
interface HomeScreenState {
  imageUri: string | null;
}

/**
 * Landing screen: lets the user pick a supplement label photo, preview it,
 * and kick off analysis. Analysis itself is stubbed out for now — it just
 * logs the picked image URI — pending backend integration
 * (POST /api/v1/scan, see backend/app/services/vision.py).
 */
const HomeScreen: React.FC = () => {
  const [imageUri, setImageUri] = useState<HomeScreenState['imageUri']>(null);

  const handleImageSelected = useCallback((uri: string | null): void => {
    setImageUri(uri);
  }, []);

  /**
   * Placeholder for the future backend call. Currently just logs the
   * local image URI to the console.
   */
  const handleAnalyze = useCallback((): void => {
    if (!imageUri) {
      return;
    }
    console.log('Analyze requested for image URI:', imageUri);
  }, [imageUri]);

  const isAnalyzeDisabled: boolean = imageUri === null;

  return (
    <SafeAreaView style={styles.safeArea}>
      <View style={styles.container}>
        <Text style={styles.title}>Supplement Label Scanner</Text>

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
          accessibilityState={{ disabled: isAnalyzeDisabled }}
        >
          <Text
            style={[
              styles.analyzeButtonText,
              isAnalyzeDisabled && styles.analyzeButtonTextDisabled,
            ]}
          >
            Analyze Label
          </Text>
        </Pressable>
      </View>
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: '#FFFFFF',
  },
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 24,
    gap: 24,
  },
  title: {
    fontSize: 22,
    fontWeight: '700',
    color: '#1A1A1A',
    marginBottom: 8,
    textAlign: 'center',
  },
  analyzeButton: {
    width: 260,
    paddingVertical: 16,
    borderRadius: 12,
    backgroundColor: '#4A90E2',
    alignItems: 'center',
    justifyContent: 'center',
  },
  analyzeButtonDisabled: {
    backgroundColor: '#CCCCCC',
  },
  analyzeButtonText: {
    fontSize: 16,
    fontWeight: '600',
    color: '#FFFFFF',
  },
  analyzeButtonTextDisabled: {
    color: '#888888',
  },
});

export default HomeScreen;
