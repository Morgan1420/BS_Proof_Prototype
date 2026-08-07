import React from "react";
import { SafeAreaView, ScrollView, StatusBar, StyleSheet, Text, View } from "react-native";
import ImageUploadSection from "./src/components/ImageUploadSection";
import ProcessingIndicator from "./src/components/ProcessingIndicator";
import ResultsView from "./src/components/ResultsView";
import ScanButton from "./src/components/ScanButton";
import { useScanPipeline } from "./src/hooks/useScanPipeline";

export default function App() {
  const [pickedImage, setPickedImage] = React.useState(null);
  const { phase, job, ingredientResults, errorMessage, startScan, reset } = useScanPipeline();

  const isBusy = phase === "uploading" || phase === "polling";

  const handleScanPress = () => {
    if (!pickedImage) return;
    startScan(pickedImage);
  };

  const handleReset = () => {
    setPickedImage(null);
    reset();
  };

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar barStyle="dark-content" />
      <ScrollView contentContainerStyle={styles.scrollContent}>
        <Text style={styles.title}>BS Proof</Text>
        <Text style={styles.subtitle}>Scan a supplement label for an evidence-based grade.</Text>

        <ImageUploadSection imageUri={pickedImage?.uri} onImageSelected={setPickedImage} disabled={isBusy} />

        <ScanButton onPress={handleScanPress} disabled={!pickedImage || isBusy} />

        <ProcessingIndicator phase={phase} ingredientResults={ingredientResults} />

        {phase === "error" && (
          <View style={styles.errorBox}>
            <Text style={styles.errorBoxText}>{errorMessage}</Text>
          </View>
        )}

        <ResultsView job={job} ingredientResults={ingredientResults} />

        {(phase === "done" || phase === "error") && (
          <Text style={styles.resetLink} onPress={handleReset}>
            Scan another label
          </Text>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: "#f9fafb" },
  scrollContent: { padding: 20, paddingBottom: 60, maxWidth: 480, width: "100%", alignSelf: "center" },
  title: { fontSize: 26, fontWeight: "800", color: "#101828" },
  subtitle: { color: "#667085", marginTop: 4, marginBottom: 20 },
  errorBox: {
    backgroundColor: "#fef3f2",
    borderColor: "#fda29b",
    borderWidth: 1,
    borderRadius: 8,
    padding: 12,
    marginBottom: 16,
  },
  errorBoxText: { color: "#b42318" },
  resetLink: { color: "#4f46e5", fontWeight: "600", textAlign: "center", marginTop: 20 },
});
