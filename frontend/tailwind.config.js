/** Tailwind theme.
 *
 * Two layers on purpose:
 *
 * 1. shadcn's SEMANTIC names (background, card, primary, border, …) resolve to
 *    the CSS variables defined in src/index.css. Stock shadcn primitives are
 *    written against these, so they inherit the Signal Desk palette untouched.
 * 2. The report's OWN names (paper, ink, crit/high/med/low) stay available for
 *    report-specific surfaces the library has no concept of — the severity
 *    scale in particular is reserved and must never be reused as a chart series.
 *
 * Values mirror `_BASE_CSS` in src/summary/builder.py. Duplicated rather than
 * imported because Tailwind needs them at build time and the Python module is
 * not reachable from Node — keep the two in sync.
 */
export default {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        primary: {
          DEFAULT: 'hsl(var(--primary))',
          foreground: 'hsl(var(--primary-foreground))',
        },
        secondary: {
          DEFAULT: 'hsl(var(--secondary))',
          foreground: 'hsl(var(--secondary-foreground))',
        },
        destructive: {
          DEFAULT: 'hsl(var(--destructive))',
          foreground: 'hsl(var(--destructive-foreground))',
        },
        muted: {
          DEFAULT: 'hsl(var(--muted))',
          foreground: 'hsl(var(--muted-foreground))',
        },
        accent: {
          DEFAULT: 'hsl(var(--accent))',
          foreground: 'hsl(var(--accent-foreground))',
        },
        popover: {
          DEFAULT: 'hsl(var(--popover))',
          foreground: 'hsl(var(--popover-foreground))',
        },
        card: {
          DEFAULT: 'hsl(var(--card))',
          foreground: 'hsl(var(--card-foreground))',
        },
        chart: {
          1: 'hsl(var(--chart-1))',
          2: 'hsl(var(--chart-2))',
          3: 'hsl(var(--chart-3))',
          4: 'hsl(var(--chart-4))',
          5: 'hsl(var(--chart-5))',
        },

        // Report-native names (Signal Desk). Severity is a RESERVED scale.
        paper: '#ECE9E2',
        ink: { DEFAULT: '#15171C', 2: '#565C69', 3: '#969BA6' },
        line: { DEFAULT: '#DED8CD', 2: '#E7E2D9' },
        crit: { DEFAULT: '#B42318', bg: '#FBEEEC', line: '#F0CFC9' },
        high: { DEFAULT: '#B25A0B', bg: '#FBF3E8', line: '#EFDCC0' },
        med: { DEFAULT: '#565C69', bg: '#EEEAE2', line: '#DCD6CB' },
        low: { DEFAULT: '#969BA6', bg: '#F3F1EA', line: '#E7E2D9' },
        ok: '#2E7D5B',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      fontFamily: {
        sans: ["'IBM Plex Sans'", 'system-ui', '-apple-system', "'Segoe UI'", 'sans-serif'],
        mono: ["'IBM Plex Mono'", 'ui-monospace', "'SF Mono'", 'Consolas', 'monospace'],
        display: ["'Archivo'", "'IBM Plex Sans'", 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        card: '0 1px 2px rgba(21,23,28,.05)',
        lift: '0 6px 20px -8px rgba(21,23,28,.18),0 1px 2px rgba(21,23,28,.06)',
      },
    },
  },
  plugins: [],
}
