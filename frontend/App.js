import React from "react";
import { SafeAreaView, ScrollView, StatusBar, StyleSheet } from "react-native";
import NavBar from "./src/components/NavBar";
import SavedIngredientsView from "./src/screens/SavedIngredientsView";
import ScanScreen from "./src/screens/ScanScreen";

export default function App() {
  const [activeTab, setActiveTab] = React.useState("scan"); // "scan" | "ingredients"

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar barStyle="dark-content" />
      <NavBar activeTab={activeTab} onChange={setActiveTab} />
      <ScrollView contentContainerStyle={styles.scrollContent}>
        {activeTab === "scan" ? <ScanScreen /> : <SavedIngredientsView />}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: "#f9fafb" },
  scrollContent: { padding: 20, paddingBottom: 60, maxWidth: 480, width: "100%", alignSelf: "center" },
});
