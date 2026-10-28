/**
 * TradingView mobile & web application theme configuration.
 *
 * Strict TradingView styling:
 * Background: #131722
 * Panel/Card: #1e222d
 * Borders: #2a2e39
 * Text: #d1d4dc (ink), muted: #787b86
 * Bullish/Green: #089981
 * Bearish/Red: #f23645
 * Blue: #2962ff
 */
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        tv: {
          bg: "#131722",
          panel: "#1e222d",
          border: "#2a2e39",
          text: "#d1d4dc",
          muted: "#787b86",
          green: "#089981",
          red: "#f23645",
          blue: "#2962ff",
          hover: "#2a2e39",
          active: "#363a45",
        },
        canvas: "#131722",
        surface: "#1e222d",
        elevated: "#242834",
        hairline: "#2a2e39",
        edge: "#363a45",
        ink: "#d1d4dc",
        "ink-dim": "#787b86",
        "ink-faint": "#50535e",
        accent: "#2962ff",
        "accent-soft": "rgba(41,98,255,0.12)",
        up: "#089981",
        "up-soft": "rgba(8,153,129,0.12)",
        down: "#f23645",
        "down-soft": "rgba(242,54,69,0.12)",
        warn: "#f59e0b",
        "warn-soft": "rgba(245,158,11,0.12)",
      },
      fontFamily: {
        sans: ["Inter", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      borderRadius: {
        DEFAULT: "4px",
        sm: "2px",
        md: "4px",
        lg: "4px",
        xl: "4px",
        "2xl": "4px",
        "3xl": "4px",
      },
      fontSize: {
        "3xs": ["9px", "12px"],
        "2xs": ["10px", "14px"],
        xs: ["11px", "15px"],
        sm: ["12px", "16px"],
        base: ["13px", "18px"],
      },
      boxShadow: {
        none: "none",
        panel: "none",
        pop: "0 4px 12px rgba(0,0,0,0.5)",
      },
      keyframes: {
        flashUp: {
          "0%": { backgroundColor: "rgba(8,153,129,0.3)" },
          "100%": { backgroundColor: "transparent" },
        },
        flashDown: {
          "0%": { backgroundColor: "rgba(242,54,69,0.3)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      animation: {
        flashUp: "flashUp 0.6s ease-out",
        flashDown: "flashDown 0.6s ease-out",
      },
    },
  },
  plugins: [],
};
