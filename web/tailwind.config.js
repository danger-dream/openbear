/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts}"],
  theme: {
    extend: {
      colors: {
        macbg: "#f5f5f7",
        macpanel: "#ffffff",
        macborder: "#e5e5ea",
        mactext: "#1d1d1f",
        macsub: "#86868b",
        macblue: "#0071e3",
      },
      fontFamily: {
        sf: ['-apple-system', 'BlinkMacSystemFont', '"SF Pro Text"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
      },
    },
  },
  plugins: [],
};
