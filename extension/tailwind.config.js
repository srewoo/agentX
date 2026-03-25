/** @type {import('tailwindcss').Config} */
export default {
  content: ["./src/**/*.{ts,tsx,html}"],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#7C3AED",
          light: "#A78BFA",
        },
        profit: "#10B981",
        loss: "#EF4444",
        warn: "#F59E0B",
        surface: "#18181B",
        panel: "#27272A",
        border: "#3F3F46",
      },
    },
  },
  plugins: [],
};
