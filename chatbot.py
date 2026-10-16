import streamlit as st
from groq import Groq
from main import load_agent, ask, GROQ_API_KEY

VOICE_MODEL = "whisper-large-v3-turbo"
# Our product words help Whisper spell them right
VOICE_HINT = "Vendor search: dumpster, porta potty, hand wash station, holding tank, washout tank, zip code."

#create the agent once, reused on every rerun
@st.cache_resource
def get_agent():
    return load_agent()

@st.cache_resource
def get_groq():
    return Groq(api_key=GROQ_API_KEY)

def transcribe(audio):
    """Turn a voice recording into text with Whisper on Groq."""
    result = get_groq().audio.transcriptions.create(
        file=("voice.wav", audio.getvalue()),
        model=VOICE_MODEL,
        language="en",
        prompt=VOICE_HINT,
    )
    return result.text.strip()

def main():
    st.title("Vendor Finder")
    st.caption("Type or say what you need, e.g. \"20 yard dumpster for 40056 within 50 miles\"")

    #messages = what is shown on screen; history = the shorter version sent to the LLM
    if "messages" not in st.session_state:
        st.session_state.messages = []
        st.session_state.history = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    submission = st.chat_input("Which product and zip code?", accept_audio=True)

    if submission:

        #a voice message is turned into text first, then handled like a typed one
        prompt, shown = submission.text, submission.text
        if submission.audio:
            try:
                with st.spinner("Listening..."):
                    prompt = transcribe(submission.audio)
            except Exception as e:
                st.error(f"Could not read the recording: {str(e)}")
                return
            shown = f"🎤 {prompt}"
        if not prompt:
            st.warning("I didn't catch that. Please try again.")
            return

        st.chat_message("user").markdown(shown)
        st.session_state.messages.append({"role": "user", "content": shown})

        try:
            with st.spinner("Searching vendors..."):
                answer = ask(get_agent(), st.session_state.history, prompt)

            #"$...$" is a math formula in Streamlit markdown, so the price signs are escaped
            answer = answer.replace("$", "\\$")
            st.chat_message("assistant").markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})

        except Exception as e:
            #drop the unanswered question from the screen (ask() already dropped it from history)
            st.session_state.messages.pop()
            st.error(f"Error: {str(e)}")

if __name__ == "__main__":
    main()
