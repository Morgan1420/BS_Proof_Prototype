import React from "react";
import { ActivityIndicator, StyleSheet, Text, View } from "react-native";
import ImageUploadSection from "../components/ImageUploadSection";
import ResultsView from "../components/ResultsView";
import ScanButton from "../components/ScanButton";
import { useScanPipeline } from "../hooks/useScanPipeline";

/**
 * "Scan" tab: pick/take a label photo, POST it to /api/scan (one Gemini
 * vision call), and show the extracted ingredients immediately -- no
 * background grading, no polling.
 */
export default function ScanScreen() {
  const [pickedImage, setPickedImage] = React.useState(null);
  const { phase, scanResult, errorMessage, startScan, reset } = useScanPipeline();

  const isBusy = phase === "scanning";

  const handleScanPress = () => {
    if (!pickedImage) return;
    startScan(pickedImage);
  };

  const handleReset = () => {
    setPickedImage(null);
    reset();
  };

  return (
    <View style={styles.container}>
      <Text style={styles.title}>BS Proof</Text>
      <Text style={styles.subtitle}>Scan a supplement label to extract its ingredients.</Text>

      <ImageUploadSection imageUri={pickedImage?.uri} onImageSelected={setPickedImage} disabled={isBusy} />

      <ScanButton
        onPress={handleScanPress}
        disabled={!pickedImage || isBusy}
        label={isBusy ? "Scanning..." : "Scan Label"}
      />

      {phase === "scanning" && (
        <View style={styles.processingRow}>
          <ActivityIndicator size="small" color="#4f46e5" />
          <Text style={styles.processingText}>Extracting ingredients...</Text>
        </View>
      )}

      {phase === "error" && (
        <View style={styles.errorBox}>
          <Text style={styles.errorBoxText}>{errorMessage}</Text>
        </View>
      )}

      <ResultsView scanResult={scanResult} />

      {(phase === "done" || phase === "error") && (
        <Text style={styles.resetLink} onPress={handleReset}>
          Scan another label
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {},
  title: { fontSize: 26, fontWeight: "800", color: "#101828" },
  subtitle: { color: "#667085", marginTop: 4, marginBottom: 20 },
  processingRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 16 },
  processingText: { color: "#344054" },
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
