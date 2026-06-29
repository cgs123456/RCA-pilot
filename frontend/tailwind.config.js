/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#EEF4FF', 100: '#D9E6FF', 200: '#BCD2FF',
          500: '#0B5FFF', 600: '#0950D9', 700: '#0742B3',
        },
        success: { 50: '#E6F4F0', 500: '#0E7C66', 600: '#0B6855' },
        warning: { 50: '#FEF3E2', 500: '#D98106', 600: '#B36B05' },
        danger: { 50: '#FDECEE', 500: '#D72638', 600: '#B81E2E' },
        ink: { 50: '#F8FAFC', 100: '#F1F5F9', 200: '#E5E7EB', 400: '#94A3B8',
               500: '#64748B', 600: '#475569', 700: '#334155', 800: '#1E293B', 900: '#0F172A' },
        src: {
          lims: '#0B5FFF', mes: '#0E7C66', ems: '#D98106',
          cmms: '#7C3AED', lms: '#B81E2E', bms: '#0284C7',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'Monaco', 'monospace'],
      },
      animation: {
        'slide-in-right': 'slideInRight 0.25s ease-out',
        'fade-in': 'fadeIn 0.2s ease-out',
        'scale-in': 'scaleIn 0.15s ease-out',
        'pulse-dot': 'pulseDot 1.4s ease-in-out infinite',
        'pulse-ring': 'pulseRing 1.8s ease-out infinite',
        shimmer: 'shimmer 1.5s linear infinite',
      },
      keyframes: {
        slideInRight: {
          '0%': { transform: 'translateX(20px)', opacity: '0' },
          '100%': { transform: 'translateX(0)', opacity: '1' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        scaleIn: {
          '0%': { transform: 'scale(0.96)', opacity: '0' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        pulseDot: {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.5', transform: 'scale(0.85)' },
        },
        pulseRing: {
          '0%': { boxShadow: '0 0 0 0 rgba(215, 38, 56, 0.4)' },
          '70%': { boxShadow: '0 0 0 8px rgba(215, 38, 56, 0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(215, 38, 56, 0)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
    },
  },
  plugins: [],
};
