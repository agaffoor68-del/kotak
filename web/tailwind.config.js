/**
 * AlphaTradePro dark trading-desk theme.
 *
 * Near-black surfaces and a single accent. Green and red are reserved for price
 * direction, so colour carries meaning rather than decoration.
 */
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#080B12",
        surface: "#0E131C",
        elevated: "#151C28",
        hairline: "#1F2937",
        edge: "#2B3648",
        ink: "#E8EDF6",
        "ink-dim": "#94A3B8",
        "ink-faint": "#5B6B82",
        accent: "#3B82F6",
        "accent-soft": "#14243D",
        up: "#22C55E",
        "up-soft": "#0B2418",
        down: "#EF4444",
        "down-soft": "#2A1214",
        warn: "#F59E0B",
        "warn-soft": "#2A1F08",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "Helvetica Neue", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "14px"],
        "3xs": ["9px", "12px"],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.03) inset, 0 8px 24px -12px rgba(0,0,0,0.7)",
        pop: "0 12px 40px -12px rgba(0,0,0,0.85)",
      },
      keyframes: {
        flash: {
          "0%": { backgroundColor: "rgba(34,197,94,0.28)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      animation: {
        flash: "flash 0.6s ease-out",
      },
    },
  },
  plugins: [],
};
