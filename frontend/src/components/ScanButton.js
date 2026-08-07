import React from "react";
import { Pressable, StyleSheet, Text } from "react-native";

/** The primary action button, positioned directly below the upload section. */
export default function ScanButton({ onPress, disabled, label = "Scan Label & Grade" }) {
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [
        styles.button,
        disabled && styles.disabled,
        pressed && !disabled && styles.pressed,
      ]}
    >
      <Text style={styles.label}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    backgroundColor: "#4f46e5",
    paddingVertical: 14,
    borderRadius: 10,
    alignItems: "center",
    marginTop: 4,
    marginBottom: 20,
  },
  pressed: { backgroundColor: "#4338ca" },
  disabled: { backgroundColor: "#c7c9f5" },
  label: { color: "#ffffff", fontWeight: "700", fontSize: 16 },
});
