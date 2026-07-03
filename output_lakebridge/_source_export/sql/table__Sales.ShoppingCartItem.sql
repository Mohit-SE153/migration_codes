CREATE TABLE [Sales].[ShoppingCartItem] (
    [ShoppingCartItemID] int NOT NULL,
    [ShoppingCartID] nvarchar(50) NOT NULL,
    [Quantity] int NOT NULL,
    [ProductID] int NOT NULL,
    [DateCreated] datetime NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
