with open("first_10000_indices.txt", "w") as f:
    for i in range(10000):
        f.write(f"{i}\n")