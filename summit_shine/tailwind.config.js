/** Summit Shine Tailwind config. Compile with: npx tailwindcss -i ./src.css -o ./static/tailwind.css --minify */
module.exports = {
  content: [
    './templates/**/*.html',
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0fdf4', 100: '#dcfce7', 200: '#bbf7d0', 300: '#86efac',
          400: '#4ade80', 500: '#22c55e', 600: '#16a34a', 700: '#15803d',
          800: '#166534', 900: '#14532d', 950: '#0a2f1c',
        },
        sage: { 400: '#a3e635', 500: '#84cc16', 600: '#65a30d' },
        bone: '#f5f3ee',
        ink: '#0a1410',
      },
      fontFamily: {
        display: ['Fraunces', 'ui-serif', 'Georgia', 'serif'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      animation: {
        'ken-burns': 'ken-burns 22s ease-out forwards',
        'fade-up': 'fade-up 1s cubic-bezier(.2,.8,.2,1) forwards',
      },
      keyframes: {
        'ken-burns': {
          '0%':   { transform: 'scale(1.0)' },
          '100%': { transform: 'scale(1.12)' },
        },
        'fade-up': {
          '0%':   { opacity: '0', transform: 'translateY(40px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
}
