import os
import discord
import random
from datetime import datetime, time, timedelta, timezone
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import NotFound

# Bot
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="/" , intents=intents, application_id="1313994355974869013")

# Firebase
cred = credentials.Certificate(os.getenv("FIREBASE_KEY_PATH"))
firebase_admin.initialize_app(cred)
db = firestore.client()

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# Plot and twist ideas 
def load_ideas(collection_name):
    try:
        doc_ref = db.collection('prompts').document(collection_name).get()
        if doc_ref.exists:
            return doc_ref.to_dict().get('ideas', [])
        else:
            return []
    except Exception as e:
        print(f"Error loading ideas: {e}")
        return []

plot_ideas = load_ideas('plot_ideas')
twist_ideas = load_ideas('twist_ideas')

# Current prompt
def save_prompt(prompt):
    db.collection('data').document('current_prompt').set({'current_prompt': prompt})

def load_prompt():
    doc = db.collection('data').document('current_prompt').get()
    return doc.to_dict().get('current_prompt') if doc.exists else None

def clear_prompt():
    db.collection('data').document('current_prompt').delete()

current_prompt = None

# Time
def load_times_from_firestore():
    doc = db.collection('data').document('times').get()
    if doc.exists:
        times = doc.to_dict()
        try:
            start_hour, start_minute = map(int, times['start_time'].split(':'))
            end_hour, end_minute = map(int, times['end_time'].split(':'))
            return time(start_hour, start_minute), time(end_hour, end_minute)
        except (ValueError, KeyError) as e:
            print(f"Error parsing times from Firestore: {e}")
            raise ValueError("Invalid time format in Firestore. Expected 'H:M'.")
    else:
        raise ValueError("Times not found in Firestore. Please add them first.")
    
START_TIME, END_TIME = load_times_from_firestore()

def get_next_sunday_end_time():
    now = datetime.now(timezone.utc)  # Use timezone-aware UTC
    next_sunday = now + timedelta(days=(6 - now.weekday()))  # Find the next Sunday
    end_time = datetime.combine(next_sunday.date(), time(16, 0, 0), tzinfo=timezone.utc)  # Set the end time at 16:00 UTC
    return end_time

def time_until_end():
    now = datetime.now(timezone.utc)  # Use timezone-aware datetime in UTC
    next_end_time = get_next_sunday_end_time()

    # Compare time only if both datetimes are timezone-aware
    if now > next_end_time:
        next_end_time = get_next_sunday_end_time()  # Reset if we've already passed this week's Sunday end
    time_remaining = next_end_time - now
    days, hours, minutes = time_remaining.days, time_remaining.seconds // 3600, (time_remaining.seconds // 60) % 60
    time_remaining_str = f"{days} days, {hours} hours, and {minutes} minutes"
    return time_remaining_str, next_end_time

# Poll
def save_poll_data(prompts, message_id):
    db.collection('data').document('poll_prompts').set({
        'prompts': prompts,
        'message_id': message_id
    })

def load_poll_data():
    doc = db.collection('data').document('poll_prompts').get()
    return doc.to_dict() if doc.exists else None

def clear_poll_data():
    db.collection('data').document('poll_prompts').delete()

async def create_poll(channel):
    poll_prompts = [f"{random.choice(plot_ideas)}, BUT {random.choice(twist_ideas)}" for _ in range(3)]

    # Create an embed for a prettier message
    embed = discord.Embed(
        title="Vote for next week's prompt!",
        color=discord.Color.dark_purple()
    )

    # Add each prompt as a field in the embed
    for i, prompt in enumerate(poll_prompts, 1):
        embed.add_field(name="", value=f"**{i}️.** {prompt}", inline=False)

    poll_message = await channel.send(embed=embed)

    # React to the message with emojis for voting
    for emoji in ["1️⃣", "2️⃣", "3️⃣"]:
        await poll_message.add_reaction(emoji)

    save_poll_data(poll_prompts, poll_message.id)

async def determine_poll_winner(channel):
    poll_data = load_poll_data()
    if not poll_data:
        print("No poll data found. Picking a random prompt.")
        return random.choice(plot_ideas) + ", BUT " + random.choice(twist_ideas)

    prompts = poll_data["prompts"]
    message_id = poll_data["message_id"]
    
    try:
        poll_message = await channel.fetch_message(message_id)

        # Count reactions
        reaction_counts = {reaction.emoji: reaction.count - 1 for reaction in poll_message.reactions}
        votes = {
            "1️⃣": reaction_counts.get("1️⃣", 0),
            "2️⃣": reaction_counts.get("2️⃣", 0),
            "3️⃣": reaction_counts.get("3️⃣", 0),
        }
        max_votes = max(votes.values())
        winners = [index for index, count in enumerate(votes.values()) if count == max_votes]

        if len(winners) > 1:  # Handle tie by random selection
            chosen_index = random.choice(winners)
        else:
            chosen_index = winners[0]

        clear_poll_data()
        return prompts[chosen_index]
    
    except discord.errors.NotFound:
        # If the poll message doesn't exist, fallback to a random choice
        clear_poll_data()
        return random.choice(plot_ideas) + ", BUT " + random.choice(twist_ideas)

# End challenge
@tasks.loop(time=END_TIME) 
async def scheduled_end():
  if datetime.now(timezone.utc).weekday() == 6:  # 6 = Sunday
    await end_challenge(bot)

@bot.tree.command(name="admin-end", description="Manually end the Weekly Queer Quill challenge (Admin Only)")
async def admin_end(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return
    
    if interaction.user.guild_permissions.administrator:
        await interaction.response.defer(ephemeral=True)
        await end_challenge(bot, interaction=interaction)
    else:
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)

async def end_challenge(bot, interaction=None):
    global current_prompt

    if current_prompt is not None:
        prompt_copy = current_prompt
        participants_mentions = []

        for guild in bot.guilds:
            role = discord.utils.get(guild.roles, name="Weekly Queer Quill")
            if role:
                participants_mentions.extend([member.mention for member in role.members])

                for member in role.members:
                    await member.remove_roles(role)

                challenge_channel = discord.utils.get(guild.channels, name="weekly-queer-quill")
                if challenge_channel:
                    participants_message = " ".join(participants_mentions[::-1]) if participants_mentions else "no participants this week."
                    embed = discord.Embed(
                        title="The Weekly Queer Quill challenge has ended",
                        description=f"*{prompt_copy}*\n\n"
                                    f"**Thank you to everyone who participated:** {participants_message}\n\n"
                                    "**See you tomorrow for a new challenge!**",
                        color=discord.Color.dark_purple()
                    )
                    await challenge_channel.send(embed=embed)
                    await create_poll(challenge_channel)

        end_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        add_to_challenge_history(end_date, prompt_copy, participants_mentions)

        current_prompt = None
        clear_prompt()

        # Send the final response to the admin
        if interaction:
            await interaction.followup.send("The challenge has been ended!", ephemeral=True)
    else:
        if interaction:
            await interaction.followup.send("No active challenge to end.", ephemeral=True)
        else:
            print("No active challenge to end.")


# Start challenge
@tasks.loop(time=START_TIME)
async def scheduled_start():
    if datetime.now(timezone.utc).weekday() == 0:  # Monday
      await start_challenge(bot)

@bot.tree.command(name="admin-start", description="Manually start the Weekly Queer Quill challenge (Admin Only)")
async def admin_start(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return
    
    if interaction.user.guild_permissions.administrator:
      await interaction.response.defer(ephemeral=True)
      await start_challenge(bot, guild=interaction.guild, interaction=interaction)
    else:
      await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)

async def start_challenge(bot, guild=None, interaction=None):
    global current_prompt

    if current_prompt is None:
        for guild in bot.guilds:
            challenge_channel = discord.utils.get(guild.channels, name="weekly-queer-quill")
            if challenge_channel:
                # Determine poll winner
                current_prompt = await determine_poll_winner(challenge_channel)
                save_prompt(current_prompt)
                next_end_time = get_next_sunday_end_time()
                end_time_str = next_end_time.strftime('%A %I:%M %p (UTC)').lstrip('0').replace(' 0', ' ')
                # Message
                embed = discord.Embed(
                    title="The Weekly Queer Quill challenge has begun!",
                    description=(
                        f"**And this week's prompt is...** *{current_prompt}*\n\n"
                        f"The challenge ends on **{end_time_str}**. Use </join:1315867324779073559> to participate!"
                    ),
                    color=discord.Color.dark_purple()
                )
                await challenge_channel.send(embed=embed)

        if interaction:
            await interaction.followup.send("The Weekly Queer Quill challenge has been started!", ephemeral=True)
    else:
        if interaction:
            await interaction.followup.send("A challenge is already active. Skipping start.", ephemeral=True)


# Commands

CHALLENGE_INACTIVE_MESSAGE = ( "The **Weekly Queer Quill** challenge is **not active** at the moment. Please wait until the next challenge starts." )

# Join
@bot.tree.command(name="join", description="Join the Weekly Queer Quill challenge")
async def join(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return
    
    global current_prompt
    if current_prompt is None:  # If no active prompt, prevent joining
        await interaction.response.send_message(CHALLENGE_INACTIVE_MESSAGE, ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name="Weekly Queer Quill")
    if role:
        if role in interaction.user.roles:
            await interaction.response.send_message(
                f"{interaction.user.mention}, you have already joined the **Weekly Queer Quill** challenge.", 
                ephemeral=True
            )
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(
                f"{interaction.user.mention} has joined the **Weekly Queer Quill** challenge!", 
            )
    else:
        await interaction.response.send_message(
            "Error. Please contact an administrator.", 
            ephemeral=True
        )

# Leave
@bot.tree.command(name="leave", description="Leave the Weekly Queer Quill challenge")
async def leave(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return

    global current_prompt
    if current_prompt is None:  # If no active prompt, prevent leaving
        await interaction.response.send_message(CHALLENGE_INACTIVE_MESSAGE, ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name="Weekly Queer Quill")
    if role:
        if role not in interaction.user.roles:
            await interaction.response.send_message(
                f"You are currently not in the **Weekly Queer Quill** challenge.", 
                ephemeral=True
            )
        else:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(
                f"You have left the **Weekly Queer Quill** challenge.", 
                ephemeral=True
            )
    else:
        await interaction.response.send_message(
            "Error. Please contact an administrator.", 
            ephemeral=True
        )

# Info
@bot.tree.command(name="info", description="Get information about the Weekly Queer Quill challenge")
async def info(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return
    
    global current_prompt  

    # Base message about the challenge
    base_message = (
        "Each week, **Weekly Queer Quill** kicks off a fun writing challenge, combining a randomly selected plot idea with a plot twist! Participate, write whatever comes to mind, however long, and share your take on the weekly prompt with the community!"
    )
    prompt_message = (
        "**Submit your own prompt with </prompt:1315867324779073564>!**"
    )

    if current_prompt is None:  # No active challenge
        embed = discord.Embed(
            title="Weekly Queer Quill",
            description=f"{base_message}\n\n{prompt_message}\n\nThe challenge is **not active** at the moment.",
            color=discord.Color.greyple() 
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:  # Active challenge
        time_remaining_str, next_end_time = time_until_end()

        embed = discord.Embed(
            title="Weekly Queer Quill",
            description=f"{base_message}\n\n\u200b",
            color=discord.Color.greyple()
        )
        # Add fields for the ongoing prompt and time remaining f"{i}️. {prompt}"
        embed.add_field(name="", value=f"**Ongoing Prompt:** {current_prompt}", inline=False)
        embed.add_field(name="", value=f"**Time Remaining:** {time_remaining_str}\n\n\u200b", inline=False)
        embed.add_field(name="", value=prompt_message, inline=False)
        embed.add_field(name="", value=f"**Use </join:1315867324779073559> to participate!**", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

# Participants
@bot.tree.command(name="participants", description="List all participants of the current Weekly Queer Quill challenge")
async def participants(interaction: discord.Interaction):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return
    
    global current_prompt
    if current_prompt is None:
        await interaction.response.send_message(CHALLENGE_INACTIVE_MESSAGE, ephemeral=True)
    else:
        role = discord.utils.get(interaction.guild.roles, name="Weekly Queer Quill")
        if role:
            members = [member.mention for member in role.members][::-1]
            if members:
                # Embed showing participants
                embed = discord.Embed(
                    description="Here are the people taking part in the current **Weekly Queer Quill**:\n\n" + " ".join(members),
                    color=discord.Color.greyple()
                )
            else:
                # Embed for no participants yet
                embed = discord.Embed(
                    description="No one has joined the **Weekly Queer Quill** yet. Be the first to participate by using </join:1315867324779073559>!",
                    color=discord.Color.greyple()
                )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            # Regular message for role not found
            await interaction.response.send_message(
                "Error. Please contact an administrator.", ephemeral=True
            )

# Prompt
@bot.tree.command(name="prompt", description="Submit your prompt idea")
@app_commands.describe(prompt="One sentence plot (Example: Two enemy spies find themselves stranded together)")
async def prompt(interaction: discord.Interaction, prompt: str):
    if not is_in_weekly_queer_quill_channel(interaction):
        await send_channel_error(interaction)
        return

    if len(prompt) > 150:
        await interaction.response.send_message(
            f"Your prompt '**{prompt}**' is too long! Please limit it to 150 characters.",
            ephemeral=True,
        )
        return

    try:
        db.collection("prompts").document("user_inputs").update(
            {"inputs": firestore.ArrayUnion([prompt])}
        )
    except NotFound:
        db.collection("prompts").document("user_inputs").set({"inputs": [prompt]})

    await interaction.response.send_message(
        "Thank you! Your prompt has been successfully submitted.", ephemeral=True
    )
    
# Challenge history
def add_to_challenge_history(end_date, prompt, participants):
    db.collection('challenge_history').add({
        'end_date': end_date,
        'prompt': prompt,
        'participants': participants
    })

def load_challenge_history():
    docs = db.collection('challenge_history').stream()
    return [doc.to_dict() for doc in docs]

# @bot.tree.command(name="admin-sync", description="Sync commands (Admin Only)")
# async def admin_sync(interaction: discord.Interaction):
#     if not is_in_weekly_queer_quill_channel(interaction):
#         await send_channel_error(interaction)
#         return
    
#     if not interaction.user.guild_permissions.administrator:
#         await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
#         return

#     try:
#         await bot.tree.sync(guild=interaction.guild)
#         await interaction.response.send_message("Bot commands successfully synced!", ephemeral=True)
#         print("Bot commands synced")
#     except Exception as e:
#         await interaction.response.send_message(f"Failed to sync commands: {e}", ephemeral=True)
#         print(f"Error syncing commands: {e}")
    
def is_in_weekly_queer_quill_channel(interaction: discord.Interaction) -> bool:
    correct_channel_id = 1315173759497273414 
    return interaction.channel.id == correct_channel_id
    
async def send_channel_error(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Error!",
        description=f"Head over to <#1315173759497273414> to do this",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)    

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    global current_prompt
    activity = discord.Activity(type=discord.ActivityType.listening, name="/info")
    await bot.change_presence(activity=activity)
    print("Activity set")
    current_prompt = load_prompt()
    scheduled_start.start()
    scheduled_end.start()
    print("Challenge schedule set")
    # Uncomment the line below if new commands are added and restart bot
    await bot.tree.sync() 

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

bot.run(TOKEN)