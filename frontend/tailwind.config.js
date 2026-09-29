/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        border: 'hsl(240 6% 90%)',
        background: 'hsl(0 0% 100%)',
        muted: 'hsl(240 5% 96%)',
        foreground: 'hsl(240 10% 4%)',
      },
    },
  },
  plugins: [],
}
