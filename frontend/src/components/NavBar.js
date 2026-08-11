import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

const TABS = [
  { key: "scan", label: "Scan" },
  { key: "ingredients", label: "Saved Ingredients" },
];

/**
 * Simple top navigation bar with two tabs: "Scan" and "Saved Ingredients".
 *
 * `activeTab` is one of TABS' `key`s; `onChange` is called with the new
 * key when a tab is pressed. Deliberately plain state-driven tabs rather
 * than a routing library -- this app has exactly two top-level screens.
 */
export default function NavBar({ activeTab, onChange }) {
  return (
    <View style={styles.container}>
      {TABS.map((tab) => {
        const isActive = tab.key === activeTab;
        return (
          <Pressable
            key={tab.key}
            onPress={() => onChange(tab.key)}
            style={[styles.tab, isActive && styles.activeTab]}
          >
            <Text style={[styles.tabLabel, isActive && styles.activeTabLabel]}>{tab.label}</Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flexDirection: "row",
    backgroundColor: "#ffffff",
    borderBottomWidth: 1,
    borderBottomColor: "#e4e7ec",
  },
  tab: {
    flex: 1,
    paddingVertical: 14,
    alignItems: "center",
    borderBottomWidth: 2,
    borderBottomColor: "transparent",
  },
  activeTab: {
    borderBottomColor: "#4f46e5",
  },
  tabLabel: {
    color: "#667085",
    fontWeight: "600",
    fontSize: 14,
  },
  activeTabLabel: {
    color: "#4f46e5",
  },
});
