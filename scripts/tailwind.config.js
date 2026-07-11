/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    '../app/templates/**/*.html',
    '../app/static/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        accent: '#00A7E1',
        'bg-page':        '#0f0f0f',
        'bg-sidebar':     '#141414',
        'bg-card':        '#1a1a1a',
        'bg-raised':      '#222222',
        'border-subtle':  '#1e1e1e',
        'border-visible': '#2d2d2d',
        'text-primary':   '#e0e0e0',
        'text-secondary': '#aaaaaa',
        'text-muted':     '#666666',
        'status-healthy': '#22c55e',
        'status-error':   '#ef4444',
        'status-warning': '#f59e0b',
        'status-info':    '#3b82f6',
      },
    },
  },
  plugins: [],
}
