/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        claude: {
          bg: '#f8f8f8',
          sidebar: '#f3f3f3',
          msgUser: '#ffffff',
          msgBot: '#f8f8f8',
          text: '#2d2d2d',
          subtext: '#767676',
          border: '#e5e5e5',
          accent: '#d97757'
        }
      }
    },
  },
  plugins: [require('@tailwindcss/typography')],
}
