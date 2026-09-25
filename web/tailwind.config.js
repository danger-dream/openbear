// Keep alpha modifiers (bg-ob-surface/70, text-macsub/75) theme-aware.
const rgb = (name) => `rgb(var(--ob-${name}-rgb) / <alpha-value>)`;
const border = (name = 'border') => ({opacityValue}) => `rgb(var(--ob-border-rgb) / calc(var(--ob-${name}-alpha) * ${opacityValue ?? 1}))`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts}"],
  theme: {
    extend: {
      colors: {
        macbg: rgb('bg'),
        macpanel: rgb('surface'),
        macborder: border(),
        mactext: rgb('text'),
        macsub: rgb('text-subtle'),
        macblue: rgb('blue'),
        'ob-bg': rgb('bg'),
        'ob-sidebar': rgb('sidebar'),
        'ob-header': rgb('header'),
        'ob-surface': rgb('surface'),
        'ob-raised': rgb('surface-raised'),
        'ob-soft': rgb('surface-soft'),
        'ob-text': rgb('text'),
        'ob-strong': rgb('text-strong'),
        'ob-subtle': rgb('text-subtle'),
        'ob-muted': rgb('text-muted'),
        'ob-inverse': rgb('text-inverse'),
        'ob-blue': rgb('blue'),
        'ob-success': rgb('success'),
        'ob-warning': rgb('warning'),
        'ob-danger': rgb('danger'),
        'ob-violet': rgb('violet'),
        'ob-orange': rgb('orange'),
        'ob-border': border(),
        'ob-border-soft': border('border-soft'),
        'ob-border-strong': border('border-strong'),
      },
      fontFamily: {
        sf: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
