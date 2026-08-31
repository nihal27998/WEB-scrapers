import numpy as np

# AND Gate Dataset
X = np.array([
    [0,0],
    [0,1],
    [1,0],
    [1,1]
])

T = np.array([0,0,0,1])

# Initialize
weights = np.zeros(2)
bias = 0
learning_rate = 0.1
epochs = 10

for epoch in range(epochs):
    print(f"\nEpoch {epoch+1}")

    for i in range(len(X)):
        net = np.dot(X[i], weights) + bias

        if net >= 0:
            output = 1
        else:
            output = 0

        error = T[i] - output

        weights = weights + learning_rate * error * X[i]
        bias = bias + learning_rate * error

        print("Input:", X[i],
              "Target:", T[i],
              "Output:", output,
              "Error:", error)

print("\nFinal Weights:", weights)
print("Final Bias:", bias)

# Testing
test = np.array([1,1])

net = np.dot(test, weights) + bias

if net >= 0:
    prediction = 1
else:
    prediction = 0

print("\nTest Input:", test)
print("Predicted Output:", prediction)